from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # Support both `python -m scripts...` and `python scripts\...`.
    from .render_aihr_plan_demo import (
        PUBLIC_DEMO_STRIP_KEYS,
        SENSITIVE_DEMO_MARKERS,
        _contract_checks,
        _missing_annual_operation_plan_fields,
        _missing_course_intake_requirements_fields,
        _missing_query_route_fields,
        _missing_recommended_path_fields,
        _missing_scope_baseline_fields,
        _missing_training_course_inventory_template_fields,
        _missing_training_necessity_review_fields,
    )
except ImportError:  # pragma: no cover - exercised when run as a script.
    from render_aihr_plan_demo import (
        PUBLIC_DEMO_STRIP_KEYS,
        SENSITIVE_DEMO_MARKERS,
        _contract_checks,
        _missing_annual_operation_plan_fields,
        _missing_course_intake_requirements_fields,
        _missing_query_route_fields,
        _missing_recommended_path_fields,
        _missing_scope_baseline_fields,
        _missing_training_course_inventory_template_fields,
        _missing_training_necessity_review_fields,
    )

PUBLIC_DEMO_SCHEMA_VALUE_MARKERS = {"source_url_or_document"}
PUBLIC_DEMO_STRIP_KEYS_LOWER = {key.lower() for key in PUBLIC_DEMO_STRIP_KEYS}
RELEASE_READINESS_SCHEMA = "aihr_release_readiness_v1"
REVIEW_ARTIFACT_READABILITY_AUDIT_SCHEMA = "review_artifact_readability_audit_v1"
NCS_FULL_MAJOR_COUNT = 24


def _public_metadata_key_markers(value: Any) -> list[str]:
    markers: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                key_text = str(key)
                if (
                    key_text in PUBLIC_DEMO_STRIP_KEYS
                    or key_text.lower() in PUBLIC_DEMO_STRIP_KEYS_LOWER
                ):
                    markers.add(key_text)
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return sorted(markers)


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.agent_queue import build_agent_queue_status_from_file
from ncs_mcp.release_labels import (
    add_blocker_display_fields,
    blocker_display_label,
    blocker_display_labels,
    blocker_display_message,
)

LEGACY_AGENT_QUEUE_ARTIFACT_DATE = "20260624"
DEFAULT_AGENT_QUEUE_ARTIFACT_DATE = datetime.now().strftime("%Y%m%d")
ARTIFACT_DATE_PATTERN = re.compile(r"20\d{6}")
ARTIFACT_STAMP_PATTERN = re.compile(r"20\d{6}(?:_[A-Za-z0-9][A-Za-z0-9_-]*)?")
ARTIFACT_STAMP_VARIANT_SUFFIXES = {
    "alias",
    "internal",
    "alias_internal",
    "all",
    "public",
    "latest",
    "current",
}
PRODUCTIZATION_STRATEGY_PATH = ROOT / "docs" / "AIHR_PRODUCTIZATION_STRATEGY.md"
DEPLOYMENT_RUNBOOK_PATH = ROOT / "docs" / "AIHR_DEPLOYMENT_RUNBOOK.md"
PRODUCTIZATION_STRATEGY_REQUIRED_MARKERS = (
    "Users And Buyers",
    "Packaging",
    "Business Model Hypotheses",
    "Deployment Responsibility Model",
    "Release Gates For Product Claims",
    "not a public qualification-recognition service",
    "API keys",
)
DEPLOYMENT_RUNBOOK_REQUIRED_MARKERS = (
    "Supported deployment modes",
    "Ownership model",
    "Release sequence",
    "Rollback and safety",
)


HUMAN_REVIEW_GATES = {
    "review_debt:candidate_definition_ratio",
    "review_debt:human_reviewed_concepts",
    "review_debt:human_reviewed_goal_links",
    "review_debt:human_reviewed_task_relations",
}


HUMAN_REVIEW_PROVENANCE_BLOCKER = "human_review:provenance_reconfirmation_required"
REVIEW_ARTIFACT_READABILITY_BLOCKER = "review_artifact:readability_audit"


REQUIRED_RELEASE_GATES = HUMAN_REVIEW_GATES | {
    "qualification:collection_coverage",
    "transition_eval:trusted_scenarios",
}


# Stable public tools that must always be exposed. Advanced ontology /
# education-integration / transition tools (plan_ncs_education_path,
# recommend_training_transition, recommend_task_transitions, get_concept_evidence)
# are hidden by default for the public release and are therefore not required here.
REQUIRED_PUBLIC_TOOLS = {
    "ncs_discover_tools",
    "ncs_execute_tool",
    "ncs_search",
    "ncs_unit_detail",
    "ncs_training",
    "ncs_analysis",
    "recommend_training_for_task",
}


REQUIRED_QUERY_ROUTER_SCENARIOS = {
    "education_system_design": {
        "tool": "plan_ncs_education_path",
        "required_params": {"current_query", "target_query"},
    },
    "training_transition": {
        "tool": "recommend_training_transition",
        "required_params": {"current_query", "target_query"},
    },
    "task_training": {
        "tool": "recommend_training_for_task",
        "required_params": {"query"},
    },
    "task_transition": {
        "tool": "recommend_task_transitions",
        "required_params": {"query"},
    },
    "evidence_analysis": {
        "tool": "ncs_analysis",
        "required_params": {"mode"},
    },
    "operator_review": {
        "tool": "get_quality_issues",
        "required_params": set(),
    },
    "structure_search": {
        "tool": "ncs_search",
        "required_params": {"query"},
    },
}


REQUIRED_DASHBOARD_CHECK_NAMES = {
    "static_artifacts",
    "live_page",
    "training_system_builder_page",
    "demo_page",
    "readiness_page",
    "review_board_page",
    "ksa_definitions_page",
    "ksa_definitions_api",
    "provenance_reconfirmation_page",
    "provenance_reconfirmation_api",
    "agent_queue_page",
    "query_router_page",
    "queue_status_page",
    "queue_status_api",
    "agent_queue_run_page",
    "agent_queue_run_api",
    "live_queue_source_path_consistency",
    "review_chain_safety",
}


REQUIRED_DASHBOARD_STATIC_ARTIFACT_NAMES = {
    "demo_html",
    "demo_alias_json",
    "demo_json",
    "guide_surface_audit_json",
    "hrd_guide_prompt_coverage_json",
    "ontology_transferability_education_audit_json",
    "queue_run_json",
    "queue_status_json",
    "query_route_contract_audit_json",
    "readiness_json",
    "review_workflow_handoff_json",
    "ncs006_element_api_checkpoint_json",
    "ncs006_element_api_checkpoint_md",
    "human_review_safe_ops_checkpoint_json",
    "human_review_safe_ops_checkpoint_md",
    "human_review_backlog_json",
    "goal_completion_audit_json",
    "api_linkage_summary_json",
    "qualification_retry_hygiene_json",
    "qualification_collection_coverage_plan_json",
    "human_review_provenance_reconfirmation_packet_json",
    "human_review_provenance_reconfirmation_decision_sheet_json",
    "human_review_provenance_reconfirmation_decision_audit_json",
    "sqf_db_readiness_checkpoint_json",
    "sqf_db_readiness_checkpoint_md",
    "overnight_ncs_sqf_work_checkpoint_json",
    "overnight_ncs_sqf_work_checkpoint_md",
}


DATE_CONSISTENT_DASHBOARD_STATIC_ARTIFACT_NAMES = {
    "demo_html",
    "demo_alias_json",
    "demo_json",
    "hrd_guide_prompt_coverage_json",
    "queue_run_json",
    "queue_status_json",
    "query_route_contract_audit_json",
    "readiness_json",
    "review_workflow_handoff_json",
    "human_review_backlog_json",
    "goal_completion_audit_json",
    "qualification_collection_coverage_plan_json",
    "human_review_provenance_reconfirmation_packet_json",
    "human_review_provenance_reconfirmation_decision_sheet_json",
    "human_review_provenance_reconfirmation_decision_audit_json",
}


STATIC_ARTIFACT_FRESHNESS_HASH_SKIP_NAMES = {
    "readiness_json",
    "queue_status_json",
    "queue_run_json",
}

RELEASE_READINESS_CYCLE_SAFE_HASH_EXCLUDED_FIELDS = (
    "artifact_lineage_contract",
    "cycle_safe_content_sha256",
    "cycle_safe_hash_excluded_fields",
    "sha256_scope",
)
RELEASE_READINESS_CYCLE_SAFE_DASHBOARD_CONTRACT_EXCLUDED_FIELDS = (
    "artifact.mtime_utc",
    "**.mtime_utc",
    "**.size_bytes",
    "**.content_sha256",
    "**.cycle_safe_content_sha256",
    "**.human_review_backlog.contract_ok",
    "**.human_review_backlog.source_hash_contract_ok",
    "**.human_review_backlog.source_hash_revalidation_ok",
    "**.human_review_backlog.source_hash_revalidation_checked_count",
    "**.human_review_backlog.source_hash_revalidation_mismatch_count",
    "**.human_review_backlog.source_hash_revalidation_issues",
)
RELEASE_READINESS_CYCLE_SAFE_DASHBOARD_CONTRACT_EXCLUDED_KEYS = {
    "mtime_utc",
    "size_bytes",
    "content_sha256",
    "cycle_safe_content_sha256",
}
RELEASE_READINESS_CYCLE_SAFE_HUMAN_REVIEW_BACKLOG_EXCLUDED_KEYS = {
    "contract_ok",
    "source_hash_contract_ok",
    "source_hash_revalidation_ok",
    "source_hash_revalidation_checked_count",
    "source_hash_revalidation_mismatch_count",
    "source_hash_revalidation_issues",
}


def _dashboard_static_artifact_role_ok(name: Any, path: Any) -> bool:
    name_text = str(name or "")
    path_obj = Path(str(path or ""))
    stem = path_obj.stem
    suffix = path_obj.suffix.lower()
    if not name_text or not stem:
        return False
    if name_text == "demo_json":
        return suffix == ".json" and stem.startswith("aihr_plan_demo_") and not stem.endswith("_alias")
    if name_text == "demo_alias_json":
        return suffix == ".json" and (
            stem.startswith("aihr_plan_demo_alias_")
            or (stem.startswith("aihr_plan_demo_") and stem.endswith("_alias"))
        )
    role_prefixes = {
        "demo_html": ("aihr_plan_demo_",),
        "readiness_json": ("aihr_release_readiness_",),
        "queue_status_json": (
            "aihr_agent_queue_status",
            "aihr_agent_work_queue_status",
            "aihr_agent_queue_release_status",
        ),
        "queue_run_json": (
            "aihr_agent_queue_run_",
            "aihr_agent_work_queue_run_",
            "aihr_agent_queue_release_run_",
        ),
        "query_route_contract_audit_json": ("query_route_contract_audit_",),
        "hrd_guide_prompt_coverage_json": ("hrd_guide_prompt_coverage",),
        "guide_surface_audit_json": ("aihr_guide_surface_audit",),
        "review_workflow_handoff_json": ("aihr_plan_review_workflow_handoff_",),
        "human_review_backlog_json": ("human_review_backlog_report_", "human_review_backlog_"),
        "goal_completion_audit_json": ("goal_completion_audit_report_", "goal_completion_audit_"),
        "api_linkage_summary_json": ("api_linkage_summary_",),
        "qualification_retry_hygiene_json": ("qualification_retry_hygiene_",),
        "qualification_collection_coverage_plan_json": (
            "qualification_collection_coverage_plan_",
        ),
        "human_review_provenance_reconfirmation_packet_json": (
            "human_review_provenance_reconfirmation_packet_",
        ),
        "human_review_provenance_reconfirmation_decision_sheet_json": (
            "human_review_provenance_reconfirmation_decision_sheet_",
        ),
        "human_review_provenance_reconfirmation_decision_audit_json": (
            "human_review_provenance_reconfirmation_decision_audit_",
        ),
        "ncs006_element_api_checkpoint_json": ("checkpoint_ncs006_element_api_status_",),
        "ncs006_element_api_checkpoint_md": ("checkpoint_ncs006_element_api_status_",),
        "human_review_safe_ops_checkpoint_json": ("human_review_safe_ops_checkpoint_",),
        "human_review_safe_ops_checkpoint_md": ("human_review_safe_ops_checkpoint_",),
        "sqf_db_readiness_checkpoint_json": ("sqf_db_readiness_checkpoint_",),
        "sqf_db_readiness_checkpoint_md": ("sqf_db_readiness_checkpoint_",),
        "overnight_ncs_sqf_work_checkpoint_json": ("overnight_ncs_sqf_work_checkpoint_",),
        "overnight_ncs_sqf_work_checkpoint_md": ("overnight_ncs_sqf_work_checkpoint_",),
    }
    if name_text == "ontology_transferability_education_audit_json":
        return suffix == ".json" and (
            stem.startswith("ontology_transferability_education_system_audit_")
            or stem.startswith("aihr_full_ncs_plan_sample_education_audit_")
        )
    prefixes = role_prefixes.get(name_text)
    if not prefixes:
        return False
    expected_suffix = ".md" if name_text.endswith("_md") else ".html" if name_text.endswith("_html") else ".json"
    return suffix == expected_suffix and any(stem.startswith(prefix) for prefix in prefixes)


REQUIRED_DASHBOARD_CHECKPOINT_JSON_ARTIFACT_NAMES = {
    "ncs006_element_api_checkpoint_json",
    "human_review_safe_ops_checkpoint_json",
    "sqf_db_readiness_checkpoint_json",
    "overnight_ncs_sqf_work_checkpoint_json",
}

DASHBOARD_PUBLIC_SHARE_ARTIFACT_NAMES = {
    "demo_alias_json",
    "demo_html",
    "demo_json",
}


REQUIRED_REVIEW_CHAIN_BLOCKED_ACTIONS = {
    "auto_approve",
    "score_boost_from_report_or_derived_diagnostics",
    "treat_report_training_as_official_learning_module",
    "write_human_reviewed_accepted_or_reviewed",
}


AGENT_ROLE_FILES = {
    "aihr-demo-runner-agent": ".agents/aihr-demo-runner-agent.md",
    "data-collection-agent": ".agents/data-collection-agent.md",
    "evaluation-agent": ".agents/evaluation-agent.md",
    "ontology-review-agent": ".agents/ontology-review-agent.md",
    "project-maintainer": "AGENTS.md",
    "task-ksa-review-agent": ".agents/task-ksa-review-agent.md",
    "training-goal-review-agent": ".agents/training-goal-review-agent.md",
}


AGENT_PRIORITY_BY_CATEGORY = {
    "dashboard_surface": 1,
    "engineering_hygiene": 1,
    "mcp_contract": 1,
    "demo_contract": 1,
    "review_artifact_quality": 1,
    "quality_report_contract": 1,
    "evaluation": 2,
    "human_review": 3,
    "data_collection": 4,
}


def _safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _queue_run_failure_issues_from_summary_and_statuses(
    summary: dict[str, Any],
    statuses: list[Any] | None = None,
) -> list[str]:
    issues: list[str] = []
    for key in ("failed_count", "acceptance_failed_count", "skipped_unsafe_count"):
        count = _safe_int(summary.get(key))
        if count is None:
            if key in summary:
                issues.append(f"{key}:not_int")
            continue
        if count:
            issues.append(f"{key}:{count}")
    for index, status_value in enumerate(statuses or []):
        status = str(status_value or "missing")
        if status in {"failed", "failed_timeout", "acceptance_failed"} or status.startswith("failed"):
            issues.append(f"run[{index}]:status={status}")
    return issues


def _dashboard_verification_lineage_contract(
    dashboard_surface_contract: dict[str, Any] | None,
    *,
    release_readiness_path: str | Path | None,
    agent_queue_path: str | Path | None,
) -> dict[str, Any]:
    expected_release_path = str(release_readiness_path or "")
    expected_queue_path = str(agent_queue_path or "")
    expected_date = _artifact_date_from_text(expected_release_path) or _artifact_date_from_text(
        expected_queue_path
    )
    if not expected_date:
        return {
            "ok": True,
            "checked": False,
            "reason": "expected release/queue paths are not dated",
        }
    artifact = (
        dashboard_surface_contract.get("artifact")
        if isinstance(dashboard_surface_contract, dict)
        and isinstance(dashboard_surface_contract.get("artifact"), dict)
        else {}
    )
    static_artifacts = artifact.get("static_artifacts")
    if not isinstance(static_artifacts, list):
        return {
            "ok": False,
            "checked": True,
            "reason": "dashboard verification static artifacts missing",
            "expected_release_path": expected_release_path,
            "expected_queue_path": expected_queue_path,
        }
    readiness_artifact = next(
        (
            item
            for item in static_artifacts
            if isinstance(item, dict) and item.get("name") == "readiness_json"
        ),
        {},
    )
    readiness_summary = (
        readiness_artifact.get("release_readiness")
        if isinstance(readiness_artifact.get("release_readiness"), dict)
        else {}
    )
    actual_release_path = str(readiness_artifact.get("path") or "")
    actual_queue_path = str(readiness_summary.get("agent_work_queue_path") or "")
    release_path_ok = _queue_source_path_matches(actual_release_path, expected_release_path)
    queue_path_ok = _queue_source_path_matches(actual_queue_path, expected_queue_path)
    dashboard_path = str(artifact.get("path") or "")
    dashboard_recorded_sha256 = _normalized_static_artifact_sha256(
        artifact.get("content_sha256")
    )
    dashboard_current_sha256 = None
    dashboard_content_hash_ok = True
    dashboard_content_hash_issue = None
    resolved_dashboard_path = _resolve_static_artifact_path(dashboard_path)
    if not dashboard_path:
        dashboard_content_hash_ok = False
        dashboard_content_hash_issue = "dashboard_verification_path_missing"
    elif dashboard_recorded_sha256 is None:
        dashboard_content_hash_ok = False
        dashboard_content_hash_issue = "dashboard_verification_sha256_missing_or_invalid"
    elif resolved_dashboard_path is None or not resolved_dashboard_path.exists():
        dashboard_content_hash_ok = False
        dashboard_content_hash_issue = "dashboard_verification_file_missing"
    else:
        try:
            dashboard_current_sha256 = _current_static_artifact_sha256(
                resolved_dashboard_path
            )
        except OSError as exc:
            dashboard_content_hash_ok = False
            dashboard_content_hash_issue = (
                f"dashboard_verification_unreadable:{type(exc).__name__}"
            )
        else:
            dashboard_content_hash_ok = dashboard_current_sha256 == dashboard_recorded_sha256
            if not dashboard_content_hash_ok:
                dashboard_content_hash_issue = "dashboard_verification_sha256_mismatch"
    return {
        "ok": release_path_ok and queue_path_ok and dashboard_content_hash_ok,
        "checked": True,
        "expected_date": expected_date,
        "dashboard_verification_path": dashboard_path,
        "dashboard_verification_content_sha256": dashboard_recorded_sha256,
        "dashboard_verification_current_content_sha256": dashboard_current_sha256,
        "dashboard_verification_mtime_utc": artifact.get("mtime_utc"),
        "dashboard_verification_content_hash_ok": dashboard_content_hash_ok,
        "dashboard_verification_content_hash_issue": dashboard_content_hash_issue,
        "expected_release_path": expected_release_path,
        "actual_release_path": actual_release_path,
        "release_path_ok": release_path_ok,
        "expected_queue_path": expected_queue_path,
        "actual_queue_path": actual_queue_path,
        "queue_path_ok": queue_path_ok,
    }


def _dashboard_checkpoint_review_gate_detail(
    *,
    name: str,
    path: str | None,
    checkpoint: dict[str, Any],
) -> dict[str, Any] | None:
    if checkpoint.get("contract_ok") is True:
        return None
    if (
        checkpoint.get("read_only_checkpoint") is not True
        or checkpoint.get("db_writes") is not False
        or checkpoint.get("status_updates") is not False
        or bool(checkpoint.get("sensitive_markers"))
        or bool(checkpoint.get("forbidden_paths"))
    ):
        return None
    pending_reconfirmation = _safe_int(
        checkpoint.get("legacy_trusted_status_rows_pending_reconfirmation")
    ) or 0
    rows_without_provenance = _safe_int(
        checkpoint.get("rows_without_packet_backed_provenance")
    ) or 0
    blank_decisions = _safe_int(
        checkpoint.get("reconfirmation_blank_decision_count")
    ) or 0
    source_rows_without_provenance = _safe_int(
        checkpoint.get("source_audit_rows_without_packet_backed_provenance")
    ) or 0
    canonical_reconfirmation_count = max(
        rows_without_provenance,
        blank_decisions,
    )
    provenance_gap = (
        checkpoint.get("review_gated") is True
        or checkpoint.get("unresolved_provenance_gap") is True
        or checkpoint.get("provenance_gap_present") is True
        or checkpoint.get("human_review_provenance_gap_present") is True
        or canonical_reconfirmation_count > 0
        or blank_decisions > 0
        or pending_reconfirmation > 0
    )
    if not provenance_gap:
        return None
    return {
        "name": name,
        "path": path,
        "schema": checkpoint.get("schema"),
        "reason": (
            checkpoint.get("review_gate_code")
            or "human_review_provenance_reconfirmation_required"
        ),
        "contract_ok": checkpoint.get("contract_ok"),
        "read_only_checkpoint": checkpoint.get("read_only_checkpoint"),
        "db_writes": checkpoint.get("db_writes"),
        "status_updates": checkpoint.get("status_updates"),
        "unresolved_provenance_gap": checkpoint.get("unresolved_provenance_gap"),
        "provenance_gap_present": checkpoint.get("provenance_gap_present"),
        "human_review_provenance_gap_present": checkpoint.get(
            "human_review_provenance_gap_present"
        ),
        "source_audit_rows_without_packet_backed_provenance": (
            source_rows_without_provenance
        ),
        "rows_without_packet_backed_provenance": canonical_reconfirmation_count,
        "canonical_provenance_reconfirmation_blocker_count": (
            canonical_reconfirmation_count
        ),
        "provenance_reconfirmation_count_source": (
            "max(source_audit_rows_without_packet_backed_provenance,"
            "reconfirmation_blank_decision_count)"
        ),
        "reconfirmation_blank_decision_count": blank_decisions,
        "legacy_trusted_status_rows_pending_reconfirmation": pending_reconfirmation,
        "automation_must_not_clear": True,
    }


def _review_status_policy_contract_ok(policy: dict[str, Any]) -> bool:
    forbidden_statuses = (
        policy.get("forbidden_automatic_statuses")
        if isinstance(policy.get("forbidden_automatic_statuses"), list)
        else []
    )
    return (
        policy.get("human_decision_required_for_status_update") is True
        and policy.get("status_update_allowed") is False
        and policy.get("db_writes") is False
        and policy.get("approval_claim") is False
        and {str(value) for value in forbidden_statuses}
        == {"human_reviewed", "accepted", "reviewed"}
    )


def _gate_by_name(quality_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(gate.get("name")): gate
        for gate in quality_report.get("gates", [])
        if isinstance(gate, dict)
    }


def build_productization_strategy_check(path: Path = PRODUCTIZATION_STRATEGY_PATH) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "name": "productization_strategy",
            "ok": False,
            "path": str(path.relative_to(ROOT) if path.is_absolute() and ROOT in path.parents else path),
            "missing_markers": list(PRODUCTIZATION_STRATEGY_REQUIRED_MARKERS),
            "detail": f"unreadable: {exc}",
        }
    missing = [marker for marker in PRODUCTIZATION_STRATEGY_REQUIRED_MARKERS if marker not in text]
    return {
        "name": "productization_strategy",
        "ok": not missing,
        "path": str(path.relative_to(ROOT) if path.is_absolute() and ROOT in path.parents else path),
        "missing_markers": missing,
        "detail": "present" if not missing else "missing required productization markers",
    }


def build_deployment_runbook_check(path: Path = DEPLOYMENT_RUNBOOK_PATH) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "name": "deployment_runbook",
            "ok": False,
            "path": str(path.relative_to(ROOT) if path.is_absolute() and ROOT in path.parents else path),
            "missing_markers": list(DEPLOYMENT_RUNBOOK_REQUIRED_MARKERS),
            "detail": f"unreadable: {exc}",
        }
    missing = [marker for marker in DEPLOYMENT_RUNBOOK_REQUIRED_MARKERS if marker not in text]
    return {
        "name": "deployment_runbook",
        "ok": not missing,
        "path": str(path.relative_to(ROOT) if path.is_absolute() and ROOT in path.parents else path),
        "missing_markers": missing,
        "detail": "present" if not missing else "missing required deployment markers",
    }


def _missing_gate_blocker(gate_name: str) -> dict[str, Any]:
    return {
        "category": "quality_report_contract",
        "name": f"missing_quality_gate:{gate_name}",
        "message": "Release readiness requires this quality gate to be present.",
        "value": None,
        "threshold": "present",
    }


def _artifact_date_from_text(text: str | None) -> str | None:
    if not text:
        return None
    matches = ARTIFACT_DATE_PATTERN.findall(str(text))
    return matches[-1] if matches else None


def _artifact_date_from_paths(*paths: Path | None) -> str:
    dates: list[str] = []
    for path in paths:
        stamp = _artifact_date_from_text(str(path) if path else None)
        if stamp and stamp not in dates:
            dates.append(stamp)
    if dates:
        return max(dates)
    return DEFAULT_AGENT_QUEUE_ARTIFACT_DATE


def _artifact_stamp_from_text(text: str | None) -> str | None:
    if not text:
        return None
    matches = ARTIFACT_STAMP_PATTERN.findall(str(text))
    return matches[-1] if matches else None


def _artifact_stamp_from_paths(*paths: Path | None) -> str:
    for path in paths:
        stamp = _artifact_stamp_from_text(str(path) if path else None)
        if stamp:
            return stamp
    return DEFAULT_AGENT_QUEUE_ARTIFACT_DATE


def _ncs006_checkpoint_preflight_sort_key(path: Path) -> tuple[str, int, float, str]:
    name = path.name.lower()
    if "_current" in name:
        variant_rank = 2
    elif "_public" in name:
        variant_rank = 1
    else:
        variant_rank = 0
    return (
        _artifact_date_from_text(str(path)) or "",
        variant_rank,
        path.stat().st_mtime,
        name,
    )


def _artifact_stamp_family(stamp: str | None, expected_stamp: str | None = None) -> str | None:
    if not stamp:
        return None
    if expected_stamp and stamp.startswith(f"{expected_stamp}_"):
        suffix = stamp[len(expected_stamp) + 1 :]
        if suffix in ARTIFACT_STAMP_VARIANT_SUFFIXES:
            return expected_stamp
    return stamp


def _date_contract_for_paths(
    paths_by_role: dict[str, Any],
    *,
    expected_date: str | None = None,
    expected_stamp: str | None = None,
) -> dict[str, Any]:
    path_dates: dict[str, dict[str, str]] = {}

    def visit(role: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, (list, tuple, set)):
            for index, item in enumerate(value):
                visit(f"{role}[{index}]", item)
            return
        path_text = str(value)
        date = _artifact_date_from_text(path_text)
        stamp = _artifact_stamp_from_text(path_text)
        if date:
            path_dates[role] = {
                "path": path_text,
                "date": date,
                "stamp": stamp or date,
                "stamp_family": _artifact_stamp_family(stamp or date, expected_stamp),
            }

    for role, value in paths_by_role.items():
        visit(role, value)

    dates = sorted({item["date"] for item in path_dates.values()})
    stamps = sorted({item["stamp"] for item in path_dates.values()})
    stamp_families = sorted({item["stamp_family"] for item in path_dates.values()})
    expected = expected_date or (dates[-1] if dates else None)
    expected_path_stamp = expected_stamp or (stamps[-1] if stamps else expected)
    expected_stamp_family = _artifact_stamp_family(expected_path_stamp, expected_path_stamp)
    mismatched_roles = sorted(
        role
        for role, item in path_dates.items()
        if expected and item["date"] != expected
    )
    mismatched_stamp_roles = sorted(
        role
        for role, item in path_dates.items()
        if expected_stamp_family and item["stamp_family"] != expected_stamp_family
    )
    ok = (
        len(dates) <= 1
        and len(stamp_families) <= 1
        and not mismatched_roles
        and not mismatched_stamp_roles
    )
    return {
        "ok": ok,
        "expected_date": expected,
        "expected_stamp": expected_path_stamp,
        "expected_stamp_family": expected_stamp_family,
        "dates": dates,
        "stamps": stamps,
        "stamp_families": stamp_families,
        "path_dates": path_dates,
        "mismatched_roles": mismatched_roles,
        "mismatched_stamp_roles": mismatched_stamp_roles,
    }


def _default_agent_queue_path(release_out: Path, artifact_date: str) -> Path:
    return release_out.parent / f"aihr_agent_queue_{artifact_date}.json"


def _artifact_dates_from_static_artifacts(
    static_artifacts: list[Any],
    *,
    names: set[str] | None = None,
) -> list[str]:
    dates: list[str] = []
    for item in static_artifacts:
        if not isinstance(item, dict):
            continue
        if names is not None and item.get("name") not in names:
            continue
        stamp = _artifact_date_from_text(str(item.get("path") or ""))
        if stamp and stamp not in dates:
            dates.append(stamp)
    return dates


def _artifact_stamp_families_from_static_artifacts(
    static_artifacts: list[Any],
    *,
    names: set[str] | None = None,
    expected_stamp: str | None = None,
) -> list[str]:
    stamp_families: list[str] = []
    for item in static_artifacts:
        if not isinstance(item, dict):
            continue
        if names is not None and item.get("name") not in names:
            continue
        stamp = _artifact_stamp_from_text(str(item.get("path") or ""))
        family = _artifact_stamp_family(stamp, expected_stamp) if stamp else None
        if family and family not in stamp_families:
            stamp_families.append(family)
    return stamp_families


def _artifact_date(value: str | None = None) -> str:
    return value or DEFAULT_AGENT_QUEUE_ARTIFACT_DATE


def _stamp_command_artifacts(command: str, artifact_date: str | None) -> str:
    return command.replace(
        LEGACY_AGENT_QUEUE_ARTIFACT_DATE,
        _artifact_date(artifact_date),
    )


def _command_option_value(command: str, option: str) -> str | None:
    tokens = _command_tokens(command)
    for index, token in enumerate(tokens):
        if token == option and index + 1 < len(tokens):
            values: list[str] = []
            for value in tokens[index + 1 :]:
                if value.startswith("--"):
                    break
                values.append(value)
            return " ".join(values) if values else None
        prefix = f"{option}="
        if token.startswith(prefix):
            return _strip_command_quotes(token[len(prefix) :])
    return None


def _strip_command_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _command_tokens(command: str) -> list[str]:
    try:
        tokens = shlex.split(str(command or ""), posix=False)
    except ValueError:
        tokens = str(command or "").split()
    return [_strip_command_quotes(token) for token in tokens]


def _quote_command_value(value: str | Path | None) -> str:
    text = str(value or "")
    if not text:
        return text
    if any(char.isspace() for char in text) or '"' in text:
        escaped = text.replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _artifact_path_text(value: str | Path | None) -> str:
    return str(value).replace("\\", "/") if value else ""


def _markdown_path_for_artifact(path_text: str | None) -> str:
    if not path_text:
        return ""
    path = Path(path_text)
    return str(path.with_suffix(".md")).replace("\\", "/")


def _transition_seedpack_path_for_quality_report(
    quality_report_path: str | Path | None,
    artifact_stamp: str,
) -> str:
    text = _artifact_path_text(quality_report_path)
    if text:
        quality_path = Path(text)
        name = quality_path.name
        for prefix in ("aihr_quality_gates_with_transition_", "quality_gates_with_transition_"):
            if name.startswith(prefix) and name.endswith(".json"):
                suffix = name[len(prefix) : -len(".json")]
                base_dir = quality_path.parent
                if str(base_dir) in {"", "."}:
                    base_dir = Path("reports")
                return _artifact_path_text(
                    base_dir / f"aihr_transition_scenario_seedpack_{suffix}.jsonl"
                )
    return f"reports/aihr_transition_scenario_seedpack_{artifact_stamp}.jsonl"


def _session_report_path_if_present(filename: str, *, posix: bool = True) -> str:
    session_path = Path("reports") / "overnight_sessions" / "readonly_refresh" / filename
    path = session_path if session_path.exists() else Path("reports") / filename
    return _artifact_path_text(path) if posix else str(path)


def _ncs006_checkpoint_path_for_artifact_stamp(
    artifact_stamp: str,
    *,
    posix: bool = True,
) -> str:
    checkpoint_date = _artifact_date_from_text(artifact_stamp) or artifact_stamp
    path = Path("reports") / f"checkpoint_ncs006_element_api_status_{checkpoint_date}_current.json"
    return _artifact_path_text(path) if posix else str(path)


def _aihr_demo_internal_names(base_name: str) -> tuple[str, str]:
    prefix = "aihr_plan_demo_"
    if base_name.startswith(prefix) and len(base_name) > len(prefix):
        suffix = base_name[len(prefix) :]
        return (
            f"aihr_plan_demo_internal_{suffix}.json",
            f"aihr_plan_demo_alias_internal_{suffix}.json",
        )
    return f"{base_name}_internal.json", f"{base_name}_alias_internal.json"


def _preflight_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _preflight_command_key(command: Any) -> str:
    return " ".join(str(command or "").replace("\\", "/").split())


def _resolve_static_artifact_path(path_text: Any) -> Path | None:
    if not path_text:
        return None
    path = Path(str(path_text))
    return path if path.is_absolute() else ROOT / path


def _resolve_queue_source_path(
    source_queue_path: Any,
    *,
    artifact_path: Path | None = None,
) -> tuple[Path, Path] | None:
    source_text = str(source_queue_path or "").strip()
    if not source_text:
        return None
    resolved_path = Path(source_text)
    if resolved_path.is_absolute():
        return resolved_path, ROOT

    candidates: list[tuple[Path, Path]] = []
    root_resolved = ROOT.resolve()

    def queue_candidate(path: Path, workspace: Path) -> tuple[Path, Path]:
        try:
            path.resolve(strict=False).relative_to(root_resolved)
            return path, ROOT
        except ValueError:
            return path, workspace

    if artifact_path is not None:
        artifact_base = artifact_path.parent.resolve()
        artifact_bundle_base = artifact_base.parent
        artifact_source_workspace = (
            ROOT if resolved_path.parent != Path(".") else artifact_base
        )
        candidates.append(
            queue_candidate(artifact_base / resolved_path, artifact_source_workspace)
        )
        if resolved_path.parent != Path("."):
            candidates.append(
                queue_candidate(
                    artifact_base / resolved_path.name,
                    artifact_source_workspace,
                )
            )
            candidates.append(
                queue_candidate(
                    artifact_bundle_base / resolved_path,
                    artifact_source_workspace,
                )
            )
    candidates.append(queue_candidate(ROOT / resolved_path, ROOT))

    seen: set[Path] = set()
    unique_candidates: list[tuple[Path, Path]] = []
    for candidate_path, workspace in candidates:
        candidate_key = candidate_path.resolve(strict=False)
        if candidate_key in seen:
            continue
        seen.add(candidate_key)
        unique_candidates.append((candidate_path, workspace))
    for candidate_path, workspace in unique_candidates:
        if candidate_path.exists():
            return candidate_path, workspace
    return unique_candidates[0] if unique_candidates else None


def _normalized_static_artifact_sha256(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", text):
        return None
    return text


def _current_static_artifact_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _dashboard_source_artifact_sha256(path: Path, snapshot: dict[str, Any]) -> str | None:
    if snapshot.get("sha256_scope") == "cycle_safe_release_readiness":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return _release_readiness_cycle_safe_sha256(payload)
    try:
        return _current_static_artifact_sha256(path)
    except OSError:
        return None


def _source_artifact_hash_revalidation(
    source_artifact_hashes: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for key in sorted(source_artifact_hashes):
        snapshot = source_artifact_hashes.get(key)
        if not isinstance(snapshot, dict):
            issue = {"key": str(key), "code": "source_hash_snapshot_not_object"}
            checks.append({**issue, "hash_matches": False})
            issues.append(issue)
            continue
        path = _resolve_static_artifact_path(snapshot.get("path"))
        expected = str(snapshot.get("sha256") or "")
        check = {
            "key": str(key),
            "path": snapshot.get("path"),
            "expected_sha256": expected,
            "sha256_scope": snapshot.get("sha256_scope"),
            "exists": bool(path and path.is_file()),
        }
        if not path or not path.is_file():
            issue = {**check, "code": "source_hash_path_missing"}
            checks.append({**issue, "hash_matches": False})
            issues.append(issue)
            continue
        actual = _dashboard_source_artifact_sha256(path, snapshot)
        check["actual_sha256"] = actual
        check["hash_matches"] = (
            bool(expected.startswith("sha256:"))
            and actual is not None
            and expected == actual
        )
        checks.append(check)
        if not check["hash_matches"]:
            issues.append({**check, "code": "source_hash_mismatch"})
    return {
        "ok": bool(source_artifact_hashes) and not issues,
        "checked_count": len(checks),
        "mismatch_count": len(issues),
        "issues": issues[:20],
        "checks": checks[:50],
    }


def _human_review_backlog_source_hash_revalidation(
    item: dict[str, Any],
) -> dict[str, Any]:
    path = _resolve_static_artifact_path(item.get("path"))
    if path and path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "checked_count": 0,
                "mismatch_count": 1,
                "issues": [{"code": "human_review_backlog_unreadable", "error": type(exc).__name__}],
            }
        source_hashes = (
            payload.get("source_artifact_hashes")
            if isinstance(payload, dict)
            and isinstance(payload.get("source_artifact_hashes"), dict)
            else {}
        )
        return _source_artifact_hash_revalidation(source_hashes)
    backlog = (
        item.get("human_review_backlog")
        if isinstance(item.get("human_review_backlog"), dict)
        else {}
    )
    if "source_hash_revalidation_ok" in backlog:
        return {
            "ok": backlog.get("source_hash_revalidation_ok") is True,
            "checked_count": _safe_int(backlog.get("source_hash_revalidation_checked_count")),
            "mismatch_count": _safe_int(backlog.get("source_hash_revalidation_mismatch_count")),
            "issues": backlog.get("source_hash_revalidation_issues") or [],
        }
    return {
        "ok": True,
        "checked_count": 0,
        "mismatch_count": 0,
        "issues": [],
        "skipped": True,
        "reason": "source_file_unavailable",
    }


def _artifact_hash_snapshot(path_text: str | Path | None) -> dict[str, Any]:
    display_path = _artifact_path_text(path_text)
    path = Path(display_path) if display_path else Path()
    resolved_path = path if path.is_absolute() else ROOT / path
    exists = bool(display_path) and resolved_path.is_file()
    size_bytes = resolved_path.stat().st_size if exists else None
    return {
        "path": display_path,
        "exists": exists,
        "non_empty": bool(size_bytes) if exists else False,
        "size_bytes": size_bytes,
        "sha256": (
            "sha256:" + hashlib.sha256(resolved_path.read_bytes()).hexdigest()
            if exists
            else None
        ),
    }


def _release_readiness_cycle_safe_projection(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    projected = copy.deepcopy(payload)
    for key in RELEASE_READINESS_CYCLE_SAFE_HASH_EXCLUDED_FIELDS:
        projected.pop(key, None)
    if "dashboard_surface_contract" in projected:
        projected["dashboard_surface_contract"] = (
            _dashboard_surface_contract_cycle_safe_projection(
                projected.get("dashboard_surface_contract")
            )
        )
    return projected


def _release_readiness_cycle_safe_sha256(payload: Any) -> str:
    return _canonical_json_sha256(_release_readiness_cycle_safe_projection(payload))


def _release_readiness_cycle_safe_hash_excluded_fields() -> list[str]:
    return list(RELEASE_READINESS_CYCLE_SAFE_HASH_EXCLUDED_FIELDS) + [
        f"dashboard_surface_contract.{key}"
        for key in RELEASE_READINESS_CYCLE_SAFE_DASHBOARD_CONTRACT_EXCLUDED_FIELDS
    ]


def _dashboard_surface_contract_cycle_safe_projection(
    value: Any,
    *,
    path: tuple[str, ...] = (),
) -> Any:
    if isinstance(value, dict):
        return {
            key: _dashboard_surface_contract_cycle_safe_projection(
                child,
                path=(*path, str(key)),
            )
            for key, child in value.items()
            if key not in RELEASE_READINESS_CYCLE_SAFE_DASHBOARD_CONTRACT_EXCLUDED_KEYS
            and not (
                "human_review_backlog" in path
                and key in RELEASE_READINESS_CYCLE_SAFE_HUMAN_REVIEW_BACKLOG_EXCLUDED_KEYS
            )
        }
    if isinstance(value, list):
        return [
            _dashboard_surface_contract_cycle_safe_projection(
                child,
                path=path,
            )
            for child in value
        ]
    return value


def _add_release_cycle_safe_hash_metadata(report: dict[str, Any]) -> None:
    report["sha256_scope"] = "cycle_safe_release_readiness"
    report["cycle_safe_hash_excluded_fields"] = (
        _release_readiness_cycle_safe_hash_excluded_fields()
    )
    report["cycle_safe_content_sha256"] = _release_readiness_cycle_safe_sha256(report)


def _static_artifact_content_hash_issue(item: dict[str, Any]) -> str | None:
    label = str(item.get("name") or item.get("path") or "unnamed")
    raw_hash = item.get("content_sha256")
    resolved_path = _resolve_static_artifact_path(item.get("path"))
    expected_hash = _normalized_static_artifact_sha256(item.get("content_sha256"))
    if expected_hash is None:
        if raw_hash in (None, ""):
            if resolved_path is not None and resolved_path.exists():
                return f"{label}:content_sha256_missing"
            return None
        return f"{label}:content_sha256_invalid"
    if resolved_path is None:
        return f"{label}:path_missing"
    try:
        current_hash = _current_static_artifact_sha256(resolved_path)
    except OSError as exc:
        return f"{label}:current_file_unreadable:{type(exc).__name__}"
    if current_hash != expected_hash:
        return f"{label}:sha256_mismatch"
    return None


def _readiness_cycle_safe_hash_issue(item: dict[str, Any]) -> str | None:
    label = str(item.get("name") or item.get("path") or "readiness_json")
    raw_hash = item.get("cycle_safe_content_sha256")
    expected_hash = _normalized_static_artifact_sha256(raw_hash)
    resolved_path = _resolve_static_artifact_path(item.get("path"))
    if expected_hash is None:
        if raw_hash in (None, ""):
            if resolved_path is not None and resolved_path.exists():
                return f"{label}:cycle_safe_content_sha256_missing"
            return None
        return f"{label}:cycle_safe_content_sha256_invalid"
    if resolved_path is None:
        return f"{label}:path_missing"
    if not resolved_path.exists():
        return f"{label}:file_missing"
    try:
        payload = json.loads(resolved_path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        return f"{label}:current_file_unreadable:{type(exc).__name__}"
    except json.JSONDecodeError:
        return f"{label}:current_file_invalid_json"
    if not isinstance(payload, dict):
        return f"{label}:current_file_not_object"
    current_hash = _release_readiness_cycle_safe_sha256(payload)
    if current_hash != expected_hash:
        return f"{label}:cycle_safe_content_sha256_mismatch"
    return None


def _canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _queue_run_public_sync_issue(item: dict[str, Any]) -> str | None:
    label = str(item.get("name") or item.get("path") or "queue_run_json")
    public_sync = (
        item.get("queue_run_public_sync")
        if isinstance(item.get("queue_run_public_sync"), dict)
        else {}
    )
    if public_sync.get("private_newer") is True:
        return f"{label}:public_stale_private_newer"
    if public_sync.get("checked") is True:
        return None
    resolved_path = _resolve_static_artifact_path(item.get("path"))
    if resolved_path is None or resolved_path.suffix.lower() != ".json":
        return None
    if not resolved_path.stem.endswith("_public"):
        return None
    private_path = resolved_path.with_name(
        resolved_path.stem[: -len("_public")] + resolved_path.suffix
    )
    if not private_path.exists():
        return None
    try:
        if private_path.stat().st_mtime > resolved_path.stat().st_mtime:
            return f"{label}:public_stale_private_newer"
    except OSError:
        return f"{label}:public_sync_unreadable"
    return None


def _queue_run_source_queue_sync_issue(item: dict[str, Any]) -> str | None:
    label = str(item.get("name") or item.get("path") or "queue_run_json")
    source_sync = (
        item.get("queue_run_source_queue_sync")
        if isinstance(item.get("queue_run_source_queue_sync"), dict)
        else {}
    )
    if source_sync.get("checked") is True:
        if source_sync.get("source_queue_matches_run") is not True:
            return f"{label}:{source_sync.get('reason') or 'source_queue_hash_mismatch'}"
        return None
    queue_run = item.get("queue_run") if isinstance(item.get("queue_run"), dict) else {}
    source_queue_path = queue_run.get("source_queue_path")
    declared_hash = str(queue_run.get("source_queue_sha256") or "").strip().lower()
    if not source_queue_path:
        return f"{label}:source_queue_path_missing"
    if not declared_hash:
        return f"{label}:source_queue_sha256_missing"
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", declared_hash):
        return f"{label}:source_queue_sha256_invalid"
    artifact_path = _resolve_static_artifact_path(item.get("path"))
    resolved = _resolve_queue_source_path(source_queue_path, artifact_path=artifact_path)
    if resolved is None:
        return f"{label}:source_queue_hash_mismatch"
    resolved_path, _workspace = resolved
    if not resolved_path.exists():
        return f"{label}:source_queue_hash_mismatch"
    try:
        current_hash = _current_static_artifact_sha256(resolved_path)
    except OSError:
        return f"{label}:source_queue_hash_mismatch"
    if current_hash != declared_hash:
        return f"{label}:source_queue_hash_mismatch"
    return None


def _queue_run_lineage_issues(item: dict[str, Any]) -> list[str]:
    label = str(item.get("name") or item.get("path") or "queue_run_json")
    queue_run = item.get("queue_run") if isinstance(item.get("queue_run"), dict) else {}
    inline_issues = queue_run.get("lineage_issues")
    if isinstance(inline_issues, list):
        return [f"{label}:{issue}" for issue in inline_issues if str(issue).strip()]
    issues: list[str] = []
    for field_name in ("source_queue_sha256", "queue_status_snapshot_sha256"):
        value = str(queue_run.get(field_name) or "").strip().lower()
        if not value:
            issues.append(f"{label}:{field_name}_missing")
        elif not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            issues.append(f"{label}:{field_name}_invalid")
    return issues


def _queue_status_snapshot_sync_issue(item: dict[str, Any]) -> str | None:
    label = str(item.get("name") or item.get("path") or "queue_run_json")
    snapshot_sync = (
        item.get("queue_status_snapshot_sync")
        if isinstance(item.get("queue_status_snapshot_sync"), dict)
        else {}
    )
    if snapshot_sync.get("checked") is True:
        if snapshot_sync.get("queue_status_snapshot_matches_run") is not True:
            return f"{label}:{snapshot_sync.get('reason') or 'queue_status_snapshot_sha256_mismatch'}"
        return None
    queue_run = item.get("queue_run") if isinstance(item.get("queue_run"), dict) else {}
    declared_hash = str(
        queue_run.get("queue_status_snapshot_sha256") or ""
    ).strip().lower()
    if not declared_hash:
        return f"{label}:queue_status_snapshot_sha256_missing"
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", declared_hash):
        return f"{label}:queue_status_snapshot_sha256_invalid"
    source_queue_path = queue_run.get("source_queue_path")
    if not source_queue_path:
        return f"{label}:source_queue_path_missing"
    artifact_path = _resolve_static_artifact_path(item.get("path"))
    resolved = _resolve_queue_source_path(source_queue_path, artifact_path=artifact_path)
    if resolved is None:
        return f"{label}:queue_status_snapshot_source_queue_missing"
    resolved_path, source_workspace = resolved
    if not resolved_path.exists():
        return f"{label}:queue_status_snapshot_source_queue_missing"
    try:
        current_status = build_agent_queue_status_from_file(
            resolved_path,
            workspace=source_workspace,
        )
        current_hash = _canonical_json_sha256(current_status)
    except ValueError:
        return f"{label}:queue_status_snapshot_unreadable"
    if current_hash != declared_hash:
        return f"{label}:queue_status_snapshot_sha256_mismatch"
    return None


def _normalized_artifact_path_key(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/")


def _hash_snapshot_for_artifact(
    hashes: dict[str, Any],
    artifact_path: Any,
) -> dict[str, Any] | None:
    artifact_key = _normalized_artifact_path_key(artifact_path)
    if not artifact_key:
        return None
    for key, snapshot in hashes.items():
        if _normalized_artifact_path_key(key) == artifact_key and isinstance(snapshot, dict):
            return snapshot
    return None


def _hash_snapshot_issue(snapshot: Any, label: str) -> str | None:
    if not isinstance(snapshot, dict):
        return f"{label}:hash_snapshot_missing"
    if snapshot.get("exists") is not True:
        return f"{label}:input_artifact_missing"
    if snapshot.get("non_empty") is not True:
        return f"{label}:input_artifact_empty"
    if _normalized_static_artifact_sha256(snapshot.get("sha256")) is None:
        return f"{label}:sha256_missing_or_invalid"
    return None


def _source_queue_from_path(
    source_queue_path: Any,
    *,
    artifact_path: Path | None = None,
) -> dict[str, Any] | None:
    resolved = _resolve_queue_source_path(source_queue_path, artifact_path=artifact_path)
    if resolved is None:
        return None
    resolved_path, _workspace = resolved
    if not resolved_path.exists():
        return {"contract_ok": False, "error": "source_queue_missing"}
    try:
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"contract_ok": False, "error": "source_queue_unreadable"}
    return payload if isinstance(payload, dict) else {"contract_ok": False, "error": "source_queue_not_object"}


def _artifact_requires_source_queue_contract(item: dict[str, Any]) -> bool:
    if _normalized_static_artifact_sha256(item.get("content_sha256")) is not None:
        return True
    resolved_artifact_path = _resolve_static_artifact_path(item.get("path"))
    return bool(resolved_artifact_path and resolved_artifact_path.exists())


def _queue_source_contract_issues(
    source_queue: Any,
    *,
    label: str,
) -> list[str]:
    if not isinstance(source_queue, dict):
        return [f"{label}:source_queue_contract_missing"]
    source_error = str(source_queue.get("error") or "").strip()
    if source_error:
        return [f"{label}:{source_error}"]
    issues: list[str] = []
    if source_queue.get("schema") != "aihr_agent_work_queue_v1":
        issues.append(f"{label}:schema_invalid")
    for field in ("report_only",):
        if source_queue.get(field) is not True:
            issues.append(f"{label}:{field}_not_true")
    for field in ("status_update_allowed", "db_writes", "approval_claim"):
        if source_queue.get(field) is not False:
            issues.append(f"{label}:{field}_not_false")
    if source_queue.get("generated_at_basis") != "artifact_date":
        issues.append(f"{label}:generated_at_basis_not_artifact_date")
    if not str(source_queue.get("generated_at") or "").strip():
        issues.append(f"{label}:generated_at_missing")
    items = source_queue.get("items")
    if not isinstance(items, list):
        issues.append(f"{label}:items_not_list")
        items = []
    item_count = _safe_int(source_queue.get("item_count"))
    if item_count is not None and item_count != len(items):
        issues.append(f"{label}:item_count_mismatch")
    input_hashes = source_queue.get("input_artifact_hashes")
    if not isinstance(input_hashes, dict):
        issues.append(f"{label}:input_artifact_hashes_missing")
        input_hashes = {}
    input_hash_count = _safe_int(source_queue.get("input_artifact_hash_count"))
    if input_hash_count is None:
        issues.append(f"{label}:input_artifact_hash_count_missing")
    elif input_hash_count != len(input_hashes):
        issues.append(f"{label}:input_artifact_hash_count_mismatch")
    for artifact_path, snapshot in input_hashes.items():
        issue = _hash_snapshot_issue(
            snapshot,
            f"{label}:input_artifact_hashes[{_normalized_artifact_path_key(artifact_path)}]",
        )
        if issue:
            issues.append(issue)
    qualification_items: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            issues.append(f"{label}:item[{index}]:not_object")
            continue
        item_id = str(item.get("id") or f"item[{index}]")
        item_hashes = item.get("input_artifact_hashes")
        if not isinstance(item_hashes, dict):
            item_hashes = {}
        input_artifacts = (
            item.get("input_artifacts")
            if isinstance(item.get("input_artifacts"), list)
            else []
        )
        prerequisite_artifacts = (
            item.get("prerequisite_artifacts")
            if isinstance(item.get("prerequisite_artifacts"), list)
            else []
        )
        input_artifact_keys = {
            _normalized_artifact_path_key(path)
            for path in input_artifacts
            if _normalized_artifact_path_key(path)
        }
        for artifact_path in prerequisite_artifacts:
            artifact_key = _normalized_artifact_path_key(artifact_path)
            if artifact_key and artifact_key not in input_artifact_keys:
                issues.append(f"{label}:{item_id}:prerequisite_not_hashed:{artifact_key}")
        for artifact_path in input_artifacts:
            artifact_key = _normalized_artifact_path_key(artifact_path)
            snapshot = _hash_snapshot_for_artifact(item_hashes, artifact_key) or _hash_snapshot_for_artifact(
                input_hashes,
                artifact_key,
            )
            issue = _hash_snapshot_issue(snapshot, f"{label}:{item_id}:input[{artifact_key}]")
            if issue:
                issues.append(issue)
        if (
            item.get("blocker") == "qualification:collection_coverage"
            or "qualification-collection-coverage" in item_id
        ):
            qualification_items.append(item)
    for item in qualification_items:
        item_id = str(item.get("id") or "qualification_collection")
        if item.get("mutation_policy") != "requires_existing_artifacts":
            issues.append(f"{label}:{item_id}:qualification_mutation_policy_invalid")
        if item.get("auto_runnable") is not False:
            issues.append(f"{label}:{item_id}:qualification_auto_runnable_not_false")
        if "collect-qualification-items" in str(item.get("command") or ""):
            issues.append(f"{label}:{item_id}:qualification_collect_command_present")
        input_artifacts = [
            _normalized_artifact_path_key(path)
            for path in item.get("input_artifacts", [])
            if _normalized_artifact_path_key(path)
        ]
        prerequisite_artifacts = [
            _normalized_artifact_path_key(path)
            for path in item.get("prerequisite_artifacts", [])
            if _normalized_artifact_path_key(path)
        ]
        required_patterns = {
            "ncs006_checkpoint": "checkpoint_ncs006_element_api_status_",
            "qualification_retry_hygiene_json": "qualification_retry_hygiene_",
            "qualification_retry_hygiene_md": "qualification_retry_hygiene_",
        }
        for required_label, pattern in required_patterns.items():
            suffix = ".md" if required_label.endswith("_md") else ".json"
            if not any(pattern in path and path.endswith(suffix) for path in prerequisite_artifacts):
                issues.append(f"{label}:{item_id}:missing_prerequisite:{required_label}")
            if not any(pattern in path and path.endswith(suffix) for path in input_artifacts):
                issues.append(f"{label}:{item_id}:missing_input_artifact:{required_label}")
    return issues


def _queue_source_contract_issues_for_artifact(
    item: dict[str, Any],
    source_queue_path: Any,
    *,
    require_source_queue_contract: bool | None = None,
) -> list[str]:
    label = str(item.get("name") or item.get("path") or "queue_source")
    source_queue = item.get("source_queue") if isinstance(item.get("source_queue"), dict) else None
    if require_source_queue_contract is None:
        require_source_queue_contract = _artifact_requires_source_queue_contract(item)
    if source_queue is None:
        if not require_source_queue_contract:
            return []
        if not source_queue_path and require_source_queue_contract:
            return [f"{label}:source_queue_path_missing"]
        artifact_path = _resolve_static_artifact_path(item.get("path"))
        source_queue = _source_queue_from_path(
            source_queue_path,
            artifact_path=artifact_path,
        )
    if source_queue is None:
        return []
    if (
        source_queue.get("error") == "source_queue_missing"
        and not require_source_queue_contract
    ):
        return []
    return _queue_source_contract_issues(source_queue, label=label)


def _readiness_lineage_issue(item: dict[str, Any], dashboard_path: Path | None) -> str | None:
    label = str(item.get("name") or item.get("path") or "readiness_json")
    resolved_readiness_path = _resolve_static_artifact_path(item.get("path"))
    if resolved_readiness_path is None or not resolved_readiness_path.exists():
        return None
    try:
        readiness = json.loads(resolved_readiness_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"{label}:artifact_lineage_unreadable:{type(exc).__name__}"
    if not isinstance(readiness, dict):
        return f"{label}:artifact_lineage_not_object"
    lineage = (
        readiness.get("artifact_lineage_contract")
        if isinstance(readiness.get("artifact_lineage_contract"), dict)
        else {}
    )
    if not lineage:
        return f"{label}:artifact_lineage_contract_missing"
    dashboard_recorded_sha256 = _normalized_static_artifact_sha256(
        lineage.get("dashboard_verification_content_sha256")
    )
    if dashboard_recorded_sha256 is None:
        return f"{label}:artifact_lineage_dashboard_verification_sha256_missing_or_invalid"
    resolved_dashboard_path = _resolve_static_artifact_path(dashboard_path)
    if resolved_dashboard_path is None or not resolved_dashboard_path.exists():
        return f"{label}:artifact_lineage_dashboard_verification_file_missing"
    try:
        current_dashboard_sha256 = _current_static_artifact_sha256(resolved_dashboard_path)
    except OSError as exc:
        return f"{label}:artifact_lineage_dashboard_verification_unreadable:{type(exc).__name__}"
    if current_dashboard_sha256 != dashboard_recorded_sha256:
        return f"{label}:artifact_lineage_dashboard_verification_sha256_mismatch"
    dashboard_lineage_path = lineage.get("dashboard_verification_path")
    if dashboard_lineage_path and not _queue_source_path_matches(
        dashboard_lineage_path,
        resolved_dashboard_path,
    ):
        return f"{label}:artifact_lineage_dashboard_verification_path_mismatch"
    return None


def _static_artifact_freshness_issues(static_artifacts: list[Any]) -> list[str]:
    issues: list[str] = []
    for item in static_artifacts:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name in STATIC_ARTIFACT_FRESHNESS_HASH_SKIP_NAMES:
            continue
        expected_hash = _normalized_static_artifact_sha256(item.get("content_sha256"))
        if expected_hash is None:
            continue
        label = name or str(item.get("path") or "unnamed")
        resolved_path = _resolve_static_artifact_path(item.get("path"))
        if resolved_path is None:
            issues.append(f"{label}:path_missing")
            continue
        try:
            current_hash = _current_static_artifact_sha256(resolved_path)
        except OSError as exc:
            issues.append(f"{label}:current_file_unreadable:{type(exc).__name__}")
            continue
        if current_hash != expected_hash:
            issues.append(f"{label}:sha256_mismatch")
    return issues


def _display_artifact_path(path_text: Any) -> str | None:
    if path_text in (None, ""):
        return None
    path = Path(str(path_text))
    try:
        resolved = path.resolve(strict=False)
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        if path.is_absolute():
            return path.name
        return path.as_posix()


def _queue_source_path_key(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if not text:
        return ""
    path = Path(text)
    try:
        resolved = path.resolve(strict=False) if path.is_absolute() else (ROOT / path).resolve(strict=False)
    except OSError:
        return text.lower()
    return resolved.as_posix().lower()


def _queue_source_path_matches(source: Any, expected: Any) -> bool:
    source_key = _queue_source_path_key(source)
    expected_key = _queue_source_path_key(expected)
    if not source_key or not expected_key:
        return False
    return source_key == expected_key


def _read_queue_status_static_artifact(item: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    inline_snapshot = item.get("queue_status") if isinstance(item.get("queue_status"), dict) else {}
    if isinstance(inline_snapshot.get("items"), list):
        return inline_snapshot, str(item.get("path") or "") or None
    resolved_path = _resolve_static_artifact_path(item.get("path"))
    if resolved_path is not None:
        try:
            payload = json.loads(resolved_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return payload, _display_artifact_path(resolved_path)
    return inline_snapshot, _display_artifact_path(item.get("path"))


def _guarded_preflight_from_status_item(
    item: dict[str, Any],
    *,
    source_path: str | None,
) -> dict[str, Any]:
    operational_guard = (
        item.get("operational_guard")
        if isinstance(item.get("operational_guard"), dict)
        else {}
    )
    safety_violations = list(
        dict.fromkeys(
            [
                *_preflight_string_list(item.get("safety_violations")),
                *_preflight_string_list(operational_guard.get("safety_violations")),
            ]
        )
    )
    preflight = {
        "schema": "aihr_guarded_api_preflight_v1",
        "source": "agent_queue_status",
        "source_path": _display_artifact_path(source_path),
        "state": item.get("state"),
        "preflight_ok": item.get("preflight_ok"),
        "can_start_automated": item.get("can_start_automated"),
        "operational_guard_status": operational_guard.get("status"),
        "api_call_allowed_now": operational_guard.get("api_call_allowed_now"),
        "element_api_call_allowed_now": operational_guard.get("element_api_call_allowed_now"),
        "qualification_retry_allowed_now": operational_guard.get("qualification_retry_allowed_now"),
        "qualification_retry_guard_reason": operational_guard.get("qualification_retry_guard_reason"),
        "next_safe_action_status": operational_guard.get("next_safe_action_status"),
        "cooldown_status": operational_guard.get("cooldown_status"),
        "cooldown_until": operational_guard.get("cooldown_until"),
        "blocked_automation": _preflight_string_list(operational_guard.get("blocked_automation")),
        "safety_violations": safety_violations,
    }
    checkpoint_path = operational_guard.get("checkpoint_path")
    if source_path:
        resolved_source_path = _resolve_static_artifact_path(source_path)
        source_parent = resolved_source_path.parent if resolved_source_path else Path(source_path).parent
        session_checkpoints = [
            path
            for path in source_parent.glob("checkpoint_ncs006_element_api_status_20*.json")
            if path.is_file()
        ]
        if session_checkpoints:
            checkpoint_path = max(
                session_checkpoints,
                key=_ncs006_checkpoint_preflight_sort_key,
            )
    if checkpoint_path:
        preflight["checkpoint_path"] = _display_artifact_path(checkpoint_path)
    return preflight


def _guarded_preflight_from_dashboard_contract(
    dashboard_surface_contract: dict[str, Any] | None,
) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {"by_command": {}, "by_blocker": {}}
    if not dashboard_surface_contract:
        return result
    artifact = dashboard_surface_contract.get("artifact")
    if not isinstance(artifact, dict):
        return result
    static_artifacts = artifact.get("static_artifacts")
    if not isinstance(static_artifacts, list):
        return result
    for static_artifact in static_artifacts:
        if not isinstance(static_artifact, dict) or static_artifact.get("name") != "queue_status_json":
            continue
        queue_status, source_path = _read_queue_status_static_artifact(static_artifact)
        status_items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for collection_name in ("items", "blocked_queue", "manual_queue"):
            collection = queue_status.get(collection_name)
            if not isinstance(collection, list):
                continue
            for status_item in collection:
                if not isinstance(status_item, dict):
                    continue
                item_id = str(status_item.get("id") or "")
                if item_id and item_id in seen_ids:
                    continue
                if item_id:
                    seen_ids.add(item_id)
                status_items.append(status_item)
        for status_item in status_items:
            if (
                status_item.get("mutation_policy") != "guarded_api_collection"
                and not isinstance(status_item.get("operational_guard"), dict)
            ):
                continue
            preflight = _guarded_preflight_from_status_item(
                status_item,
                source_path=source_path,
            )
            command_key = _preflight_command_key(status_item.get("command"))
            blocker = str(status_item.get("blocker") or "")
            if command_key:
                result["by_command"][command_key] = preflight
            if blocker:
                result["by_blocker"][blocker] = preflight
    return result


def _provenance_reconfirmation_static_artifact_issues(
    static_artifacts: list[Any],
) -> dict[str, Any]:
    expected_names = {
        "human_review_provenance_reconfirmation_packet_json",
        "human_review_provenance_reconfirmation_decision_sheet_json",
        "human_review_provenance_reconfirmation_decision_audit_json",
    }
    present_names: set[str] = set()
    bad_artifacts: list[str] = []
    lineage_hashes: dict[str, str] = {}
    for item in static_artifacts:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name not in expected_names:
            continue
        present_names.add(name)
        if item.get("role_contract_ok") is not True:
            bad_artifacts.append(str(item.get("path") or name))
        if name == "human_review_provenance_reconfirmation_packet_json":
            packet = (
                item.get("human_review_provenance_reconfirmation_packet")
                if isinstance(item.get("human_review_provenance_reconfirmation_packet"), dict)
                else {}
            )
            if packet.get("contract_ok") is not True:
                bad_artifacts.append(str(item.get("path") or name))
            if item.get("content_sha256"):
                lineage_hashes["packet"] = str(item.get("content_sha256"))
        elif name == "human_review_provenance_reconfirmation_decision_sheet_json":
            sheet = (
                item.get("human_review_provenance_reconfirmation_decision_sheet")
                if isinstance(item.get("human_review_provenance_reconfirmation_decision_sheet"), dict)
                else {}
            )
            if sheet.get("contract_ok") is not True:
                bad_artifacts.append(str(item.get("path") or name))
            if sheet.get("source_packet_sha256"):
                lineage_hashes["decision_sheet"] = str(sheet.get("source_packet_sha256"))
        elif name == "human_review_provenance_reconfirmation_decision_audit_json":
            audit = (
                item.get("human_review_provenance_reconfirmation_decision_audit")
                if isinstance(item.get("human_review_provenance_reconfirmation_decision_audit"), dict)
                else {}
            )
            if audit.get("contract_ok") is not True:
                bad_artifacts.append(str(item.get("path") or name))
            if audit.get("source_packet_sha256"):
                lineage_hashes["decision_audit"] = str(audit.get("source_packet_sha256"))
    missing_names = sorted(expected_names - present_names)
    hash_values = [value for value in lineage_hashes.values() if value]
    lineage_mismatch = len(hash_values) == len(expected_names) and len(set(hash_values)) > 1
    issues = {
        "bad_artifacts": sorted(set(bad_artifacts)),
        "missing_artifacts": missing_names,
        "lineage_hashes": lineage_hashes,
        "lineage_mismatch": lineage_mismatch,
    }
    issues["issue_count"] = (
        len(issues["bad_artifacts"])
        + len(issues["missing_artifacts"])
        + (1 if lineage_mismatch else 0)
    )
    return issues


def _human_review_provenance_blockers_from_dashboard_contract(
    dashboard_surface_contract: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not dashboard_surface_contract:
        return []
    artifact = dashboard_surface_contract.get("artifact")
    if not isinstance(artifact, dict):
        return []
    static_artifacts = artifact.get("static_artifacts")
    if not isinstance(static_artifacts, list):
        return []
    review_chain_summary = (
        artifact.get("review_chain_safety_summary")
        if isinstance(artifact.get("review_chain_safety_summary"), dict)
        else {}
    )

    blockers: list[dict[str, Any]] = []
    for static_artifact in static_artifacts:
        if (
            not isinstance(static_artifact, dict)
            or static_artifact.get("name") != "human_review_safe_ops_checkpoint_json"
        ):
            continue
        checkpoint = (
            static_artifact.get("checkpoint")
            if isinstance(static_artifact.get("checkpoint"), dict)
            else {}
        )
        checkpoint_rows_without_provenance = _safe_int(
            checkpoint.get("rows_without_packet_backed_provenance")
        )
        checkpoint_source_rows_without_provenance = _safe_int(
            checkpoint.get("source_audit_rows_without_packet_backed_provenance")
        )
        source_rows_without_provenance = (
            checkpoint_source_rows_without_provenance
            if checkpoint_source_rows_without_provenance is not None
            else checkpoint_rows_without_provenance
        )
        summary_rows_without_provenance = _safe_int(
            review_chain_summary.get("rows_without_packet_backed_provenance")
        )
        canonical_reconfirmation_count = max(
            value
            for value in (
                checkpoint_rows_without_provenance or 0,
                summary_rows_without_provenance or 0,
            )
        )
        checkpoint_blank_reconfirmation_decisions = _safe_int(
            checkpoint.get("reconfirmation_blank_decision_count")
        )
        summary_blank_reconfirmation_decisions = _safe_int(
            review_chain_summary.get("reconfirmation_blank_decision_count")
        )
        summary_blank_decisions = _safe_int(review_chain_summary.get("blank_decision_count"))
        blank_reconfirmation_decisions = max(
            value
            for value in (
                checkpoint_blank_reconfirmation_decisions or 0,
                summary_blank_reconfirmation_decisions or 0,
                summary_blank_decisions or 0,
            )
        )
        checkpoint_pending_reconfirmation = _safe_int(
            checkpoint.get("legacy_trusted_status_rows_pending_reconfirmation")
        )
        checkpoint_trusted_row_count = _safe_int(checkpoint.get("trusted_row_count"))
        summary_pending_reconfirmation = _safe_int(
            review_chain_summary.get("legacy_status_needs_reconfirmation_count")
        )
        summary_pending_decisions = _safe_int(
            review_chain_summary.get("pending_decision_count")
        )
        legacy_trusted_rows_pending_reconfirmation = max(
            checkpoint_pending_reconfirmation or 0,
            checkpoint_trusted_row_count or 0,
        )
        review_chain_reconfirmation_backlog = max(
            summary_pending_reconfirmation or 0,
            summary_pending_decisions or 0,
        )
        canonical_reconfirmation_count = max(
            canonical_reconfirmation_count,
            blank_reconfirmation_decisions or 0,
            review_chain_reconfirmation_backlog,
        )
        provenance_gap_present = checkpoint.get("provenance_gap_present") is True
        unresolved_gap_present = checkpoint.get("unresolved_provenance_gap") is True
        has_unresolved_gap = (
            provenance_gap_present
            or unresolved_gap_present
            or (canonical_reconfirmation_count or 0) > 0
            or (blank_reconfirmation_decisions or 0) > 0
        )
        if not has_unresolved_gap:
            continue
        blockers.append(
            {
                "category": "human_review",
                "name": HUMAN_REVIEW_PROVENANCE_BLOCKER,
                "message": (
                    "Trusted human-review rows require packet-backed provenance "
                    "reconfirmation before they can support release claims."
                ),
                "value": canonical_reconfirmation_count,
                "threshold": "0 unresolved provenance gaps",
                "details": {
                    "artifact": static_artifact.get("path"),
                    "checkpoint_schema": checkpoint.get("schema"),
                    "checkpoint_ok": checkpoint.get("ok"),
                    "checkpoint_contract_ok": checkpoint.get("contract_ok"),
                    "legacy_trusted_status_rows_pending_reconfirmation": (
                        legacy_trusted_rows_pending_reconfirmation
                    ),
                    "legacy_status_needs_reconfirmation_count": (
                        summary_pending_reconfirmation
                    ),
                    "pending_decision_count": summary_pending_decisions,
                    "source_audit_rows_without_packet_backed_provenance": (
                        source_rows_without_provenance or 0
                    ),
                    "rows_without_packet_backed_provenance": (
                        source_rows_without_provenance or 0
                    ),
                    "checkpoint_rows_without_packet_backed_provenance": (
                        checkpoint_rows_without_provenance
                    ),
                    "review_chain_rows_without_packet_backed_provenance": (
                        summary_rows_without_provenance
                    ),
                    "canonical_provenance_reconfirmation_blocker_count": (
                        canonical_reconfirmation_count
                    ),
                    "provenance_reconfirmation_count_source": (
                        "max(source_audit_rows_without_packet_backed_provenance,"
                        "reconfirmation_blank_decision_count,"
                        "legacy_status_needs_reconfirmation_count,"
                        "pending_decision_count)"
                    ),
                    "provenance_gap_present": provenance_gap_present,
                    "unresolved_provenance_gap": unresolved_gap_present,
                    "reconfirmation_blank_decision_count": blank_reconfirmation_decisions,
                    "read_only_checkpoint": checkpoint.get("read_only_checkpoint"),
                    "db_writes": checkpoint.get("db_writes"),
                    "status_updates": checkpoint.get("status_updates"),
                },
            }
        )
    artifact_issues = _provenance_reconfirmation_static_artifact_issues(static_artifacts)
    if artifact_issues["issue_count"]:
        blockers.append(
            {
                "category": "human_review",
                "name": HUMAN_REVIEW_PROVENANCE_BLOCKER,
                "message": (
                    "Provenance reconfirmation packet, decision sheet, and decision "
                    "audit must stay contract-valid and source-packet hash aligned."
                ),
                "value": artifact_issues["issue_count"],
                "threshold": "0 provenance proofset artifact issues",
                "details": {
                    "bad_artifacts": artifact_issues["bad_artifacts"],
                    "missing_artifacts": artifact_issues["missing_artifacts"],
                    "lineage_hashes": artifact_issues["lineage_hashes"],
                    "lineage_mismatch": artifact_issues["lineage_mismatch"],
                    "read_only_checkpoint": True,
                    "db_writes": False,
                    "status_updates": False,
                },
            }
        )
    return blockers


def _preflight_for_action(
    action: dict[str, Any],
    guarded_preflight: dict[str, dict[str, dict[str, Any]]] | None,
) -> dict[str, Any] | None:
    if not guarded_preflight:
        return None
    command_key = _preflight_command_key(action.get("command"))
    blocker = str(action.get("blocker") or "")
    preflight = guarded_preflight.get("by_command", {}).get(command_key)
    if preflight is None and _is_guarded_qualification_api_command(command_key):
        preflight = guarded_preflight.get("by_blocker", {}).get(blocker)
    return dict(preflight) if isinstance(preflight, dict) else None


def _dashboard_static_artifact_path(
    dashboard_surface_contract: dict[str, Any] | None,
    name: str,
) -> str | None:
    if not isinstance(dashboard_surface_contract, dict):
        return None
    artifact = dashboard_surface_contract.get("artifact")
    if not isinstance(artifact, dict):
        return None
    static_artifacts = artifact.get("static_artifacts")
    if not isinstance(static_artifacts, list):
        return None
    for item in static_artifacts:
        if not isinstance(item, dict) or item.get("name") != name:
            continue
        path = item.get("path")
        if isinstance(path, str) and path.strip():
            return path
    return None


def build_release_next_actions(
    blockers: list[dict[str, Any]],
    *,
    artifact_date: str | None = None,
    dashboard_static_artifact_dir: str | Path | None = None,
    quality_report_path: str | Path | None = None,
    quality_report_markdown_path: str | Path | None = None,
    release_readiness_markdown_path: str | Path | None = None,
    review_priority_report_path: str | Path | None = None,
    review_priority_markdown_path: str | Path | None = None,
    guarded_preflight: dict[str, dict[str, dict[str, Any]]] | None = None,
    ncs006_checkpoint_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    artifact_stamp = _artifact_date(artifact_date)
    output_dir = Path(dashboard_static_artifact_dir) if dashboard_static_artifact_dir else Path("reports")

    def action_artifact(filename: str) -> str:
        return str(output_dir / filename)

    release_readiness_markdown_path_text = str(release_readiness_markdown_path) if release_readiness_markdown_path else ""
    release_readiness_markdown_path_default = action_artifact(
        f"aihr_release_readiness_{artifact_stamp}.md"
    )
    quality_report_path_text = str(quality_report_path) if quality_report_path else ""
    quality_report_path_default = action_artifact(
        f"aihr_quality_gates_with_transition_{artifact_stamp}.json"
    )
    quality_report_markdown_path_text = (
        str(quality_report_markdown_path) if quality_report_markdown_path else ""
    )
    quality_report_markdown_path_default = action_artifact(
        f"aihr_quality_gates_with_transition_{artifact_stamp}.md"
    )
    transition_seedpack_path = _transition_seedpack_path_for_quality_report(
        quality_report_path_text or quality_report_path_default,
        artifact_stamp,
    )
    transition_seedpack_fallback = f"reports/aihr_transition_scenario_seedpack_{artifact_stamp}.jsonl"
    if transition_seedpack_path == transition_seedpack_fallback:
        transition_seedpack_path = action_artifact(
            f"aihr_transition_scenario_seedpack_{artifact_stamp}.jsonl"
        )
    review_priority_report_path_text = str(review_priority_report_path) if review_priority_report_path else ""
    review_priority_report_path_default = action_artifact(
        f"aihr_review_priority_{artifact_stamp}.json"
    )
    review_priority_markdown_path_text = str(review_priority_markdown_path) if review_priority_markdown_path else ""
    review_priority_markdown_path_default = action_artifact(
        f"aihr_review_priority_{artifact_stamp}.md"
    )
    ncs006_checkpoint_path_text = (
        str(ncs006_checkpoint_path) if ncs006_checkpoint_path else ""
    )
    dashboard_static_dir_arg = (
        f"--static-artifact-dir {_quote_command_value(dashboard_static_artifact_dir)} "
        if dashboard_static_artifact_dir
        else ""
    )
    for blocker in blockers:
        name = str(blocker.get("name") or "")
        action: dict[str, str]
        if name.startswith("missing_quality_gate:"):
            action = {
                "blocker": name,
                "owner": "evaluation-agent",
                "action": "Regenerate the quality-gates report with the required gate present before judging release readiness.",
                "command": (
                    "python scripts\\ncs_harness.py quality-gates --include-transition-eval "
                    "--transition-limit 5 --transition-scenario-limit 20 "
                    f"--out {_quote_command_value(action_artifact(f'aihr_quality_gates_with_transition_{artifact_stamp}.json'))} "
                    f"--markdown-out {_quote_command_value(action_artifact(f'aihr_quality_gates_with_transition_{artifact_stamp}.md'))}"
                ),
            }
        elif name in {
            "review_debt:candidate_definition_ratio",
            "review_debt:human_reviewed_concepts",
        }:
            action = {
                "blocker": name,
                "owner": "ontology-review-agent",
                "action": "Prepare and review high-priority ontology concept definitions; do not mark concepts human_reviewed without human approval.",
                "command": (
                    "python scripts\\ncs_harness.py export-ontology-definition-seedpack --limit 100 --per-issue-type-limit 50 "
                    f"--out {_quote_command_value(action_artifact(f'aihr_ontology_definition_review_seedpack_{artifact_stamp}.jsonl'))} "
                    f"--markdown-out {_quote_command_value(action_artifact(f'aihr_ontology_definition_review_seedpack_{artifact_stamp}.md'))} "
                    f"--csv-out {_quote_command_value(action_artifact(f'aihr_ontology_definition_review_seedpack_{artifact_stamp}.csv'))} "
                    f"--source-report-path {_quote_command_value(review_priority_markdown_path_text or review_priority_markdown_path_default)}"
                ),
            }
        elif name == "review_debt:human_reviewed_goal_links":
            action = {
                "blocker": name,
                "owner": "training-goal-review-agent",
                "action": "Review training-goal to KSA links and transition scenario evidence that affect visible recommendations.",
                "command": (
                    "python scripts\\ncs_harness.py review-triage "
                    f"--quality-report {_quote_command_value(quality_report_path_text or quality_report_path_default)} "
                    f"--review-priority-report {_quote_command_value(review_priority_report_path_text or review_priority_report_path_default)} "
                    f"--transition-seedpack {_quote_command_value(transition_seedpack_path)} "
                    f"--out {_quote_command_value(action_artifact(f'aihr_review_triage_{artifact_stamp}.json'))} "
                    f"--markdown-out {_quote_command_value(action_artifact(f'aihr_review_triage_{artifact_stamp}.md'))}"
                ),
            }
        elif name == "review_debt:human_reviewed_task_relations":
            action = {
                "blocker": name,
                "owner": "task-ksa-review-agent",
                "action": "Prepare a blocker-ranked task-KSA review seedpack; do not promote review statuses without a human decision.",
                "command": (
                    "python scripts\\ncs_harness.py export-review-seedpack --limit 100 --per-issue-type-limit 10 "
                    "--issue-types ontology_training_goal_link_human_review_required,hr_training_goal_link_human_review_required,"
                    "ontology_task_ksa_relation_human_review_required,hr_core_concept_human_review_required,"
                    "ontology_core_concept_human_review_required "
                    f"--out {_quote_command_value(action_artifact(f'aihr_review_seedpack_blocker_ranked_{artifact_stamp}.jsonl'))} "
                    f"--markdown-out {_quote_command_value(action_artifact(f'aihr_review_seedpack_blocker_ranked_{artifact_stamp}.md'))} "
                    f"--csv-out {_quote_command_value(action_artifact(f'aihr_review_seedpack_blocker_ranked_{artifact_stamp}.csv'))} "
                    f"--source-report-path {_quote_command_value(review_priority_markdown_path_text or review_priority_markdown_path_default)}"
                ),
            }
        elif name == HUMAN_REVIEW_PROVENANCE_BLOCKER:
            packet_json = action_artifact(
                f"human_review_provenance_reconfirmation_packet_{artifact_stamp}.json"
            )
            packet_md = action_artifact(
                f"human_review_provenance_reconfirmation_packet_{artifact_stamp}.md"
            )
            packet_html = action_artifact(
                f"human_review_provenance_reconfirmation_packet_{artifact_stamp}.html"
            )
            sheet_json = action_artifact(
                f"human_review_provenance_reconfirmation_decision_sheet_{artifact_stamp}.json"
            )
            sheet_csv = action_artifact(
                f"human_review_provenance_reconfirmation_decision_sheet_{artifact_stamp}.csv"
            )
            sheet_html = action_artifact(
                f"human_review_provenance_reconfirmation_decision_sheet_{artifact_stamp}.html"
            )
            sheet_md = action_artifact(
                f"human_review_provenance_reconfirmation_decision_sheet_{artifact_stamp}.md"
            )
            audit_json = action_artifact(
                f"human_review_provenance_reconfirmation_decision_audit_{artifact_stamp}.json"
            )
            audit_md = action_artifact(
                f"human_review_provenance_reconfirmation_decision_audit_{artifact_stamp}.md"
            )
            action = {
                "blocker": name,
                "owner": "ontology-review-agent",
                "action": (
                    "Export the provenance reconfirmation packet, blank decision sheet, "
                    "and decision audit for legacy trusted-status labels; keep all decisions "
                    "blank until a human reviewer acts."
                ),
                "command": (
                    "python scripts\\ncs_harness.py export-human-review-provenance-reconfirmation-proofset "
                    f"--out {_quote_command_value(packet_json)} "
                    f"--markdown-out {_quote_command_value(packet_md)} "
                    f"--html-out {_quote_command_value(packet_html)} "
                    f"--decision-sheet-out {_quote_command_value(sheet_json)} "
                    f"--decision-sheet-csv-out {_quote_command_value(sheet_csv)} "
                    f"--decision-sheet-html-out {_quote_command_value(sheet_html)} "
                    f"--decision-sheet-markdown-out {_quote_command_value(sheet_md)} "
                    f"--decision-audit-out {_quote_command_value(audit_json)} "
                    f"--decision-audit-markdown-out {_quote_command_value(audit_md)}"
                ),
            }
        elif name == "qualification:collection_coverage":
            checkpoint_arg = ncs006_checkpoint_path_text or _ncs006_checkpoint_path_for_artifact_stamp(
                artifact_stamp,
                posix=False,
            )
            coverage_json = action_artifact(
                f"qualification_collection_coverage_plan_{artifact_stamp}.json"
            )
            coverage_md = action_artifact(
                f"qualification_collection_coverage_plan_{artifact_stamp}.md"
            )
            coverage_csv = action_artifact(
                f"qualification_collection_coverage_plan_{artifact_stamp}.csv"
            )
            hygiene_json = action_artifact(
                f"qualification_retry_hygiene_{artifact_stamp}.json"
            )
            action = {
                "blocker": name,
                "owner": "data-collection-agent",
                "action": (
                    f"Confirm {_quote_command_value(hygiene_json)} first, then regenerate the read-only "
                    "qualification coverage plan and keep direct API collection out of automation until "
                    "an operator chooses a guarded timing window."
                ),
                "command": (
                    "python scripts\\ncs_harness.py qualification-coverage-plan "
                    "--target-ratio 0.9 --batch-size 100 "
                    f"--ncs006-checkpoint-path {_quote_command_value(checkpoint_arg)} "
                    f"--out {_quote_command_value(coverage_json)} "
                    f"--markdown-out {_quote_command_value(coverage_md)} "
                    f"--csv-out {_quote_command_value(coverage_csv)}"
                ),
            }
        elif name == "transition_eval:trusted_scenarios":
            action = {
                "blocker": name,
                "owner": "evaluation-agent",
                "action": "Prepare transition scenario review seedpack; human reviewers must decide which scenarios become trusted.",
                "command": (
                    "python scripts\\ncs_harness.py export-transition-scenario-seedpack --scenario-limit 20 --recommendation-limit 5 "
                    f"--out {_quote_command_value(action_artifact(f'aihr_transition_scenario_seedpack_{artifact_stamp}.jsonl'))} "
                    f"--markdown-out {_quote_command_value(action_artifact(f'aihr_transition_scenario_seedpack_{artifact_stamp}.md'))} "
                    f"--source-report-path {_quote_command_value(quality_report_markdown_path_text or quality_report_markdown_path_default)}"
                ),
            }
        elif name == "aihr_demo_contract":
            action = {
                "blocker": name,
                "owner": "aihr-demo-runner-agent",
                "action": "Regenerate public demo JSON/HTML and rerun demo contract checks.",
                "command": (
                    "python scripts\\ncs_harness.py run-aihr-plan-demo "
                    f"--out-dir {_quote_command_value(output_dir)} "
                    f"--base-name aihr_plan_demo_{artifact_stamp}"
                ),
            }
        elif name == "aihr_dashboard_surface":
            action = {
                "blocker": name,
                "owner": "aihr-demo-runner-agent",
                "action": "Start the dashboard, rerun live surface verification, and inspect failed endpoint or matrix checks.",
                "command": (
                    "python scripts\\ncs_harness.py verify-aihr-dashboard --base-url http://127.0.0.1:8765 "
                    f"{dashboard_static_dir_arg}"
                    f"--out {_quote_command_value(action_artifact(f'aihr_dashboard_surface_verification_{artifact_stamp}.json'))} "
                    f"--markdown-out {_quote_command_value(action_artifact(f'aihr_dashboard_surface_verification_{artifact_stamp}.md'))}"
                ),
            }
        elif name == REVIEW_ARTIFACT_READABILITY_BLOCKER:
            action = {
                "blocker": name,
                "owner": "project-maintainer",
                "action": "Regenerate the report-only review artifact readability audit and inspect encoding/display findings before relying on review packets.",
                "command": (
                    "python scripts\\ncs_harness.py audit-review-artifact-readability --reports-dir reports "
                    f"--out {_quote_command_value(action_artifact(f'review_artifact_readability_audit_{artifact_stamp}.json'))} "
                    f"--markdown-out {_quote_command_value(action_artifact(f'review_artifact_readability_audit_{artifact_stamp}.md'))}"
                ),
            }
        else:
            action = {
                "blocker": name,
                "owner": "project-maintainer",
                "action": "Inspect this blocker and add a concrete remediation command.",
                "command": "python scripts\\ncs_harness.py inspect",
            }
        action["command"] = _stamp_command_artifacts(
            str(action.get("command") or ""),
            artifact_date,
        )
        preflight = _preflight_for_action(action, guarded_preflight)
        if preflight:
            action["preflight"] = preflight
        action.setdefault("blocker_display_label", blocker_display_label(name))
        if action["blocker"] not in seen:
            actions.append(action)
            seen.add(action["blocker"])
    return actions


def _queue_item_id(owner: str, blocker: str, index: int) -> str:
    raw = f"{owner}-{blocker}".lower()
    slug = "".join(char if char.isalnum() else "-" for char in raw)
    slug = "-".join(part for part in slug.split("-") if part)
    return f"aihr-{index:02d}-{slug[:72]}"


def _command_policy(command: str, category: str) -> dict[str, Any]:
    mutates = _is_guarded_qualification_api_command(command)
    regenerates = any(
        marker in command
        for marker in (
            "quality-gates",
            "review-priority",
            "review-triage",
            "export-review-seedpack",
            "export-ontology-definition-seedpack",
            "export-transition-scenario-seedpack",
            "export-human-review-provenance-reconfirmation-packet",
            "export-human-review-provenance-reconfirmation-proofset",
            "audit-review-artifact-readability",
            "run-aihr-plan-demo",
            "verify-aihr-dashboard",
        )
    )
    requires_human_decision = category in {"human_review", "evaluation"} and "export-" in command
    if _command_invokes(command, "export-ontology-definition-seedpack"):
        requires_human_decision = False
    if _command_invokes(command, "export-review-seedpack"):
        requires_human_decision = False
    if _command_invokes(command, "export-human-review-provenance-reconfirmation-packet"):
        requires_human_decision = False
    if _command_invokes(command, "export-human-review-provenance-reconfirmation-proofset"):
        requires_human_decision = False
    if mutates:
        mutation_policy = "guarded_api_collection"
        auto_runnable = False
    elif regenerates:
        mutation_policy = "regenerate_reports_only"
        auto_runnable = True
    else:
        mutation_policy = "inspect_only"
        auto_runnable = False
    if _command_invokes(command, "verify-aihr-dashboard"):
        # Dashboard verification reads the latest queue-run evidence, so running it
        # inside the same agent-queue execution creates a self-referential failure.
        mutation_policy = "requires_existing_artifacts"
        auto_runnable = False
    if category == "data_collection" and _command_invokes(command, "qualification-coverage-plan"):
        mutation_policy = "requires_existing_artifacts"
        auto_runnable = False
    packet_only_provenance_command = _command_invokes(
        command,
        "export-human-review-provenance-reconfirmation-packet",
    ) and not _command_invokes(
        command,
        "export-human-review-provenance-reconfirmation-proofset",
    )
    if packet_only_provenance_command:
        mutation_policy = "requires_existing_artifacts"
        auto_runnable = False
    if category == "evaluation" and _command_invokes(command, "export-transition-scenario-seedpack"):
        auto_runnable = False
        mutation_policy = "requires_human_decision"
    if requires_human_decision:
        auto_runnable = False
    return {
        "auto_runnable": auto_runnable,
        "mutation_policy": mutation_policy,
        "requires_human_decision": requires_human_decision,
    }


def _command_invokes(command: str, subcommand: str) -> bool:
    return any(token == subcommand for token in _command_tokens(command))


def _is_guarded_qualification_api_command(command: str) -> bool:
    return _command_invokes(command, "retry-qualification-errors") or _command_invokes(
        command,
        "collect-qualification-items",
    )


def _explicit_output_artifacts_for_command(command: str) -> list[str]:
    artifacts: list[str] = []
    for option in (
        "--out",
        "--markdown-out",
        "--csv-out",
        "--html-out",
        "--jsonl-out",
        "--review-jsonl-out",
        "--review-csv-out",
        "--decision-sheet-out",
        "--decision-sheet-csv-out",
        "--decision-sheet-html-out",
        "--decision-sheet-markdown-out",
        "--decision-audit-out",
        "--decision-audit-markdown-out",
    ):
        value = _command_option_value(command, option)
        if value:
            artifacts.append(_artifact_path_text(value))
    return list(dict.fromkeys(artifacts))


def _expected_artifacts_for_command(command: str, artifact_date: str | None = None) -> list[str]:
    explicit_artifacts = _explicit_output_artifacts_for_command(command)
    if explicit_artifacts:
        return explicit_artifacts
    artifacts: list[str] = []
    date = _artifact_date(artifact_date or _artifact_date_from_text(command))
    output_dir = _command_option_value(command, "--out-dir") or "reports"
    if _command_invokes(command, "quality-gates"):
        artifacts.extend(
            [
                f"reports/aihr_quality_gates_with_transition_{date}.json",
                f"reports/aihr_quality_gates_with_transition_{date}.md",
            ]
        )
    if _command_invokes(command, "review-priority"):
        artifacts.extend(
            [
                f"reports/aihr_review_priority_{date}.json",
                f"reports/aihr_review_priority_{date}.md",
            ]
        )
    if _command_invokes(command, "review-triage"):
        artifacts.extend(
            [
                f"reports/aihr_review_triage_{date}.json",
                f"reports/aihr_review_triage_{date}.md",
            ]
        )
    if _command_invokes(command, "export-review-seedpack"):
        if "blocker_ranked" in command:
            artifacts.append(f"reports/aihr_review_seedpack_blocker_ranked_{date}.jsonl")
            if "--markdown-out" in command:
                artifacts.append(f"reports/aihr_review_seedpack_blocker_ranked_{date}.md")
            if "--csv-out" in command:
                artifacts.append(f"reports/aihr_review_seedpack_blocker_ranked_{date}.csv")
        else:
            artifacts.append(f"reports/aihr_review_seedpack_{date}.jsonl")
            if "--markdown-out" in command:
                artifacts.append(f"reports/aihr_review_seedpack_{date}.md")
            if "--csv-out" in command:
                artifacts.append(f"reports/aihr_review_seedpack_{date}.csv")
    if _command_invokes(command, "export-ontology-definition-seedpack"):
        artifacts.append(f"reports/aihr_ontology_definition_review_seedpack_{date}.jsonl")
        if "--markdown-out" in command:
            artifacts.append(f"reports/aihr_ontology_definition_review_seedpack_{date}.md")
        if "--csv-out" in command:
            artifacts.append(f"reports/aihr_ontology_definition_review_seedpack_{date}.csv")
    if _command_invokes(command, "export-transition-scenario-seedpack"):
        artifacts.append(f"reports/aihr_transition_scenario_seedpack_{date}.jsonl")
        if "--markdown-out" in command:
            artifacts.append(f"reports/aihr_transition_scenario_seedpack_{date}.md")
    if _command_invokes(command, "export-human-review-provenance-reconfirmation-packet"):
        artifacts.append(f"reports/human_review_provenance_reconfirmation_packet_{date}.json")
        if "--markdown-out" in command:
            artifacts.append(f"reports/human_review_provenance_reconfirmation_packet_{date}.md")
        if "--html-out" in command:
            artifacts.append(f"reports/human_review_provenance_reconfirmation_packet_{date}.html")
    if _command_invokes(command, "export-human-review-provenance-reconfirmation-proofset"):
        artifacts.extend(
            [
                f"reports/human_review_provenance_reconfirmation_packet_{date}.json",
                f"reports/human_review_provenance_reconfirmation_packet_{date}.md",
                f"reports/human_review_provenance_reconfirmation_packet_{date}.html",
                f"reports/human_review_provenance_reconfirmation_decision_sheet_{date}.json",
                f"reports/human_review_provenance_reconfirmation_decision_sheet_{date}.csv",
                f"reports/human_review_provenance_reconfirmation_decision_sheet_{date}.html",
                f"reports/human_review_provenance_reconfirmation_decision_sheet_{date}.md",
                f"reports/human_review_provenance_reconfirmation_decision_audit_{date}.json",
                f"reports/human_review_provenance_reconfirmation_decision_audit_{date}.md",
            ]
        )
    if _command_invokes(command, "audit-review-artifact-readability"):
        artifacts.append(f"reports/review_artifact_readability_audit_{date}.json")
        if "--markdown-out" in command:
            artifacts.append(f"reports/review_artifact_readability_audit_{date}.md")
    if _command_invokes(command, "run-aihr-plan-demo"):
        base_name = _command_option_value(command, "--base-name") or f"aihr_plan_demo_{date}"
        internal_name, alias_internal_name = _aihr_demo_internal_names(base_name)
        artifacts.extend(
            [
                _artifact_path_text(Path(output_dir) / f"{base_name}.json"),
                _artifact_path_text(Path(output_dir) / f"{base_name}_alias.json"),
                _artifact_path_text(Path(output_dir) / internal_name),
                _artifact_path_text(Path(output_dir) / alias_internal_name),
                _artifact_path_text(Path(output_dir) / f"{base_name}.html"),
            ]
        )
    if _command_invokes(command, "verify-aihr-dashboard"):
        artifacts.extend(
            [
                f"reports/aihr_dashboard_surface_verification_{date}.json",
                f"reports/aihr_dashboard_surface_verification_{date}.md",
            ]
        )
    return artifacts


def _prerequisite_artifacts_for_command(command: str, artifact_date: str | None = None) -> list[str]:
    artifacts: list[str] = []
    date = _artifact_date(artifact_date or _artifact_date_from_text(command))
    if _command_invokes(command, "review-triage"):
        quality_report = _artifact_path_text(_command_option_value(command, "--quality-report"))
        review_priority_report = _artifact_path_text(
            _command_option_value(command, "--review-priority-report")
        )
        transition_seedpack = _artifact_path_text(
            _command_option_value(command, "--transition-seedpack")
        )
        artifacts.extend(
            [
                quality_report or f"reports/aihr_quality_gates_with_transition_{date}.json",
                review_priority_report or f"reports/aihr_review_priority_{date}.json",
                transition_seedpack or f"reports/aihr_transition_scenario_seedpack_{date}.jsonl",
            ]
        )
    if _is_guarded_qualification_api_command(command):
        artifacts.extend(
            [
                _session_report_path_if_present(f"qualification_retry_hygiene_{date}.json"),
                _session_report_path_if_present(f"qualification_retry_hygiene_{date}.md"),
                _session_report_path_if_present(f"qualification_collection_coverage_plan_{date}.json"),
                _session_report_path_if_present(f"qualification_collection_coverage_plan_{date}.md"),
                _session_report_path_if_present(f"qualification_collection_coverage_plan_{date}.csv"),
            ]
        )
    if _command_invokes(command, "qualification-coverage-plan"):
        checkpoint_path = _artifact_path_text(
            _command_option_value(command, "--ncs006-checkpoint-path")
        )
        if not checkpoint_path:
            checkpoint_path = _ncs006_checkpoint_path_for_artifact_stamp(date)
        artifacts.extend(
            [
                checkpoint_path,
                _session_report_path_if_present(f"qualification_retry_hygiene_{date}.json"),
                _session_report_path_if_present(f"qualification_retry_hygiene_{date}.md"),
            ]
        )
    return list(dict.fromkeys(artifact for artifact in artifacts if artifact))


def _prerequisite_commands_for_command(command: str, artifact_date: str | None = None) -> list[str]:
    commands: list[str] = []
    date = _artifact_date(artifact_date or _artifact_date_from_text(command))
    if _command_invokes(command, "review-triage"):
        quality_report = _artifact_path_text(_command_option_value(command, "--quality-report"))
        review_priority_report = _artifact_path_text(
            _command_option_value(command, "--review-priority-report")
        ) or f"reports/aihr_review_priority_{date}.json"
        transition_seedpack = _artifact_path_text(
            _command_option_value(command, "--transition-seedpack")
        ) or f"reports/aihr_transition_scenario_seedpack_{date}.jsonl"
        quality_report_markdown = (
            _markdown_path_for_artifact(quality_report)
            if quality_report
            else f"reports/aihr_quality_gates_with_transition_{date}.md"
        )
        commands.append(
            "python scripts\\ncs_harness.py review-priority "
            f"--out {_quote_command_value(review_priority_report)} "
            f"--markdown-out {_quote_command_value(_markdown_path_for_artifact(review_priority_report))}"
        )
        commands.append(
            "python scripts\\ncs_harness.py export-transition-scenario-seedpack "
            "--scenario-limit 20 --recommendation-limit 5 "
            f"--out {_quote_command_value(transition_seedpack)} "
            f"--markdown-out {_quote_command_value(_markdown_path_for_artifact(transition_seedpack))} "
            f"--source-report-path {_quote_command_value(quality_report_markdown)}"
        )
    if _is_guarded_qualification_api_command(command):
        hygiene_json = _session_report_path_if_present(
            f"qualification_retry_hygiene_{date}.json",
            posix=False,
        )
        hygiene_md = _session_report_path_if_present(
            f"qualification_retry_hygiene_{date}.md",
            posix=False,
        )
        checkpoint_path = _artifact_path_text(_command_option_value(command, "--ncs006-checkpoint-path"))
        if not checkpoint_path:
            checkpoint_path = _ncs006_checkpoint_path_for_artifact_stamp(
                date,
                posix=False,
            )
        commands.append(
            "python scripts\\ncs_harness.py qualification-retry-hygiene "
            f"--ncs006-checkpoint-path {_quote_command_value(checkpoint_path)} "
            f"--out {_quote_command_value(hygiene_json)} "
            f"--markdown-out {_quote_command_value(hygiene_md)}"
        )
        coverage_json = _session_report_path_if_present(
            f"qualification_collection_coverage_plan_{date}.json",
            posix=False,
        )
        coverage_md = _session_report_path_if_present(
            f"qualification_collection_coverage_plan_{date}.md",
            posix=False,
        )
        coverage_csv = _session_report_path_if_present(
            f"qualification_collection_coverage_plan_{date}.csv",
            posix=False,
        )
        commands.append(
            "python scripts\\ncs_harness.py qualification-coverage-plan "
            "--target-ratio 0.9 --batch-size 100 "
            f"--ncs006-checkpoint-path {_quote_command_value(checkpoint_path)} "
            f"--out {_quote_command_value(coverage_json)} "
            f"--markdown-out {_quote_command_value(coverage_md)} "
            f"--csv-out {_quote_command_value(coverage_csv)}"
        )
    return commands


def _queue_status_raw_auto_start_violations(queue_status: dict[str, Any]) -> list[str]:
    if not isinstance(queue_status, dict):
        return []
    violations: list[str] = []
    for section_name in ("execution_order", "items", "manual_queue", "blocked_queue", "fallback_actions"):
        section = queue_status.get(section_name)
        if not isinstance(section, list):
            continue
        for item in section:
            if not isinstance(item, dict):
                continue
            claims_auto_start = item.get("can_start_automated") is True or section_name in {
                "execution_order",
                "fallback_actions",
            }
            if not claims_auto_start:
                continue
            state = item.get("state")
            mutation_policy = item.get("mutation_policy")
            requires_human_decision = item.get("requires_human_decision") is True
            if (
                state != "ready_to_start"
                or mutation_policy != "regenerate_reports_only"
                or requires_human_decision
            ):
                item_id = str(item.get("id") or item.get("owner") or "unknown")
                violations.append(
                    f"{section_name}:{item_id}:state={state},policy={mutation_policy},"
                    f"requires_human_decision={str(requires_human_decision).lower()},"
                    f"can_start_automated={str(item.get('can_start_automated')).lower()}"
                )
    return violations


def _acceptance_checks_for_command(command: str, category: str) -> list[str]:
    checks = ["Record commands run and touched files in the handoff."]
    if _command_invokes(command, "quality-gates"):
        checks.append("Quality-gates JSON contains all required release gates.")
    if _command_invokes(command, "review-priority"):
        checks.append("Review-priority report lists target context and does not mark human_reviewed.")
    if _command_invokes(command, "review-triage"):
        checks.append("Confirm prerequisite review-priority, quality-gates, and transition seedpack artifacts exist before running.")
        checks.append("Review-triage report reads existing artifacts only and does not mutate review statuses.")
    if _command_invokes(command, "export-review-seedpack") or _command_invokes(
        command,
        "export-ontology-definition-seedpack",
    ) or _command_invokes(
        command,
        "export-transition-scenario-seedpack",
    ):
        checks.append("Seedpack is JSONL and every item remains candidate/review-pending unless a human decides otherwise.")
    if _command_invokes(command, "export-human-review-provenance-reconfirmation-packet"):
        checks.append("Reconfirmation packet remains export-only and does not update trusted review statuses.")
    if _command_invokes(command, "export-human-review-provenance-reconfirmation-proofset"):
        checks.append(
            "Reconfirmation packet, blank decision sheet, and decision audit share the same source packet hash."
        )
        checks.append(
            "Proofset remains report-only and does not update trusted review statuses."
        )
    if _command_invokes(command, "audit-review-artifact-readability"):
        checks.append("Readability audit remains report-only and keeps status_update_allowed, db_writes, and approval_claim false.")
        checks.append("Do not treat readability findings as semantic human approval or rejection decisions.")
    if _command_invokes(command, "export-ontology-definition-seedpack"):
        checks.append("Seedpack issue_types are limited to ontology definition blockers and status_update_allowed remains false.")
    if _is_guarded_qualification_api_command(command):
        checks.append("Run qualification-retry-hygiene before qualification API collection to capture coverage gap and broad retry risk.")
        checks.append("Run qualification-coverage-plan before broad qualification API collection to size guarded batches.")
        checks.append("Qualification API command uses NCS006 and rate-limit guards and does not print service keys.")
        checks.append("Run qualification-summary after collection to capture coverage movement.")
    if _command_invokes(command, "qualification-coverage-plan"):
        checks.append("Confirm qualification-retry-hygiene and checkpoint evidence before running qualification-coverage-plan.")
        checks.append("Coverage plan remains read-only and does not call external APIs or update the database.")
        checks.append("Do not run collect-qualification-items from this queue item; guarded API collection requires explicit operator timing.")
    if _command_invokes(command, "run-aihr-plan-demo"):
        checks.append("Public demo contract passes and internal JSON artifacts are not used as public proof links.")
    if _command_invokes(command, "verify-aihr-dashboard"):
        checks.append("Dashboard verification checks live planner and queue status endpoints without mutating source data.")
    if category in {"dashboard_surface", "mcp_contract", "demo_contract", "engineering_hygiene", "quality_report_contract"}:
        checks.append("Run focused unit tests for the changed script or contract surface.")
    return checks


QUEUE_ITEM_INPUT_OPTIONS = (
    "--quality-report",
    "--review-priority-report",
    "--source-report-path",
    "--transition-seedpack",
    "--ncs006-checkpoint-path",
)


def _queue_item_input_artifacts(
    command: str,
    prerequisite_artifacts: list[str],
) -> list[str]:
    artifacts: list[str] = []
    for option in QUEUE_ITEM_INPUT_OPTIONS:
        value = _artifact_path_text(_command_option_value(command, option))
        if value:
            artifacts.append(value)
    artifacts.extend(str(artifact) for artifact in prerequisite_artifacts if artifact)
    return list(dict.fromkeys(artifacts))


def _queue_input_artifact_hashes(artifacts: list[str]) -> dict[str, dict[str, Any]]:
    return {
        artifact: _artifact_hash_snapshot(artifact)
        for artifact in artifacts
        if artifact
    }


def _aggregate_queue_input_hashes(
    items: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    aggregate: dict[str, dict[str, Any]] = {}
    for item in items:
        item_hashes = item.get("input_artifact_hashes")
        if not isinstance(item_hashes, dict):
            continue
        for path_text, snapshot in item_hashes.items():
            if isinstance(snapshot, dict):
                aggregate[str(path_text)] = dict(snapshot)
    return dict(sorted(aggregate.items()))


def _queue_generated_at_for_artifact_date(artifact_date: str | None) -> str:
    date_token = _artifact_date_from_text(_artifact_date(artifact_date))
    if not date_token:
        date_token = datetime.now(timezone.utc).strftime("%Y%m%d")
    return (
        f"{date_token[:4]}-{date_token[4:6]}-{date_token[6:8]}"
        "T00:00:00+00:00"
    )


def build_agent_work_queue(
    report: dict[str, Any],
    *,
    artifact_date: str | None = None,
) -> dict[str, Any]:
    blockers_by_name = {
        str(blocker.get("name")): blocker
        for blocker in report.get("blockers", [])
        if isinstance(blocker, dict)
    }
    items: list[dict[str, Any]] = []
    merged_by_execution_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for index, action in enumerate(report.get("next_actions") or [], start=1):
        blocker_name = str(action.get("blocker") or "")
        owner = str(action.get("owner") or "project-maintainer")
        command = str(action.get("command") or "")
        blocker = blockers_by_name.get(blocker_name, {})
        category = str(blocker.get("category") or "unknown")
        policy = _command_policy(command, category)
        prerequisite_artifacts = _prerequisite_artifacts_for_command(command, artifact_date)
        prerequisite_commands = _prerequisite_commands_for_command(command, artifact_date)
        expected_artifacts = _expected_artifacts_for_command(command, artifact_date)
        input_artifacts = _queue_item_input_artifacts(command, prerequisite_artifacts)
        prerequisite_gated_auto_allowed = _command_invokes(command, "review-triage")
        if prerequisite_artifacts and not prerequisite_gated_auto_allowed:
            policy["auto_runnable"] = False
            if policy["mutation_policy"] == "regenerate_reports_only":
                policy["mutation_policy"] = "requires_existing_artifacts"
        item = {
            "id": _queue_item_id(owner, blocker_name, index),
            "priority": AGENT_PRIORITY_BY_CATEGORY.get(category, 5),
            "owner": owner,
            "agent_file": AGENT_ROLE_FILES.get(owner, "AGENTS.md"),
            "blocker": blocker_name,
            "blocker_display_label": (
                action.get("blocker_display_label")
                or blocker.get("display_label")
                or blocker_display_label(blocker_name)
            ),
            "covered_blockers": [blocker_name],
            "covered_blocker_display_labels": [blocker_display_label(blocker_name)],
            "blocker_category": category,
            "action": action.get("action") or "",
            "command": command,
            "prerequisite_artifacts": prerequisite_artifacts,
            "prerequisite_commands": prerequisite_commands,
            "input_artifacts": input_artifacts,
            "input_artifact_hashes": _queue_input_artifact_hashes(input_artifacts),
            "expected_artifacts": expected_artifacts,
            "acceptance_checks": _acceptance_checks_for_command(command, category),
            **policy,
        }
        preflight = action.get("preflight")
        if isinstance(preflight, dict):
            item["preflight"] = dict(preflight)
        execution_key = (
            owner,
            command,
            tuple(prerequisite_artifacts),
            tuple(expected_artifacts),
            item["mutation_policy"],
        )
        existing = merged_by_execution_key.get(execution_key)
        if existing is None:
            merged_by_execution_key[execution_key] = item
            items.append(item)
            continue
        if blocker_name not in existing["covered_blockers"]:
            existing["covered_blockers"].append(blocker_name)
        display_label = blocker_display_label(blocker_name)
        if display_label not in existing["covered_blocker_display_labels"]:
            existing["covered_blocker_display_labels"].append(display_label)
        existing["priority"] = min(existing["priority"], item["priority"])
        for check in item["acceptance_checks"]:
            if check not in existing["acceptance_checks"]:
                existing["acceptance_checks"].append(check)
        for input_artifact in item.get("input_artifacts") or []:
            if input_artifact not in existing.setdefault("input_artifacts", []):
                existing["input_artifacts"].append(input_artifact)
        existing["input_artifact_hashes"] = _queue_input_artifact_hashes(
            existing.get("input_artifacts") or []
        )
        if action.get("action") and str(action.get("action")) not in str(existing.get("action") or ""):
            existing["action"] = f"{existing.get('action')} Covers also: {action.get('action')}"
    items.sort(key=lambda item: (item["priority"], item["id"]))
    input_artifact_hashes = _aggregate_queue_input_hashes(items)
    return {
        "schema": "aihr_agent_work_queue_v1",
        "generated_at": _queue_generated_at_for_artifact_date(artifact_date),
        "generated_at_basis": "artifact_date",
        "source": "release_readiness",
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "release_ready": bool(report.get("release_ready")),
        "engineering_hygiene_ok": bool(report.get("engineering_hygiene_ok")),
        "item_count": len(items),
        "input_artifact_hashes": input_artifact_hashes,
        "input_artifact_hash_count": len(input_artifact_hashes),
        "items": items,
        "global_guardrails": [
            "Read AGENTS.md, ARCHITECTURE.md, docs/HARNESS_ENGINEERING.md, docs/NCS_MCP_PRD.md, and the assigned agent file before acting.",
            "Do not set human_reviewed, accepted, or reviewed statuses without an explicit human decision.",
            "Do not use SQF or NCS study modules as active recommendation evidence unless the user explicitly reactivates them.",
            "Do not print service keys or .env values.",
            "Treat the 2026 HR NCS guide as a planning rubric and validation framework, not as operational source training data.",
        ],
    }


def write_agent_queue_markdown(queue: dict[str, Any], out_path: Path) -> None:
    lines = [
        "# AI-HR Agent Work Queue",
        "",
        f"- schema: {queue.get('schema')}",
        f"- generated_at: {queue.get('generated_at')}",
        f"- generated_at_basis: {queue.get('generated_at_basis')}",
        f"- report_only: {str(queue.get('report_only')).lower()}",
        f"- status_update_allowed: {str(queue.get('status_update_allowed')).lower()}",
        f"- db_writes: {str(queue.get('db_writes')).lower()}",
        f"- approval_claim: {str(queue.get('approval_claim')).lower()}",
        f"- release_ready: {str(queue.get('release_ready')).lower()}",
        f"- engineering_hygiene_ok: {str(queue.get('engineering_hygiene_ok')).lower()}",
        f"- item_count: {queue.get('item_count')}",
        f"- input_artifact_hash_count: {queue.get('input_artifact_hash_count')}",
        "- automation_contract: final auto-start eligibility is computed by `agent-queue-status` as "
        "`can_start_automated=true` with `mutation_policy=regenerate_reports_only`.",
        "",
        "## Guardrails",
        "",
    ]
    for guardrail in queue.get("global_guardrails") or []:
        lines.append(f"- {guardrail}")
    lines.extend(["", "## Items", ""])
    items = queue.get("items") or []
    if not items:
        lines.append("- none")
    for item in items:
        blocker_name = str(item.get("blocker") or "")
        blocker_label = str(item.get("blocker_display_label") or blocker_display_label(blocker_name))
        covered_labels = item.get("covered_blocker_display_labels")
        if not isinstance(covered_labels, list) or not covered_labels:
            covered_labels = blocker_display_labels(
                item.get("covered_blockers") or [blocker_name]
            )
        lines.extend(
            [
                f"### {blocker_label}",
                "",
                f"- id: {item.get('id')}",
                f"- owner: {item.get('owner')}",
                f"- agent_file: {item.get('agent_file')}",
                f"- priority: {item.get('priority')}",
                f"- blocker: {blocker_name}",
                f"- blocker_display_label: {blocker_label}",
                f"- covered_blockers: {', '.join(item.get('covered_blockers') or [str(item.get('blocker') or '')])}",
                f"- covered_blocker_display_labels: {', '.join(str(value) for value in covered_labels)}",
                f"- category: {item.get('blocker_category')}",
                f"- auto_runnable: {str(item.get('auto_runnable')).lower()}",
                f"- mutation_policy: {item.get('mutation_policy')}",
                f"- requires_human_decision: {str(item.get('requires_human_decision')).lower()}",
                "- queue_status_can_start_automated: "
                + str((item.get("preflight") or {}).get("can_start_automated")).lower(),
                f"- action: {item.get('action')}",
                f"- command: `{_markdown_guarded_command(item)}`",
                *(
                    [
                        f"- preflight_state: {(item.get('preflight') or {}).get('state')}",
                        f"- preflight_ok: {str((item.get('preflight') or {}).get('preflight_ok')).lower()}",
                        f"- api_call_allowed_now: {str((item.get('preflight') or {}).get('api_call_allowed_now')).lower()}",
                        "- qualification_retry_allowed_now: "
                        + str((item.get("preflight") or {}).get("qualification_retry_allowed_now")).lower(),
                        "- qualification_retry_guard_reason: "
                        + str((item.get("preflight") or {}).get("qualification_retry_guard_reason")),
                        f"- next_safe_action_status: {(item.get('preflight') or {}).get('next_safe_action_status')}",
                        f"- checkpoint_path: {(item.get('preflight') or {}).get('checkpoint_path')}",
                        "- preflight_safety_violations: "
                        + (
                            ", ".join((item.get("preflight") or {}).get("safety_violations") or [])
                            or "none"
                        ),
                    ]
                    if isinstance(item.get("preflight"), dict)
                    else []
                ),
                "",
                "Input artifacts:",
            ]
        )
        input_artifacts = item.get("input_artifacts") or []
        if not input_artifacts:
            lines.append("- none")
        for artifact in input_artifacts:
            snapshot = (
                item.get("input_artifact_hashes", {}).get(artifact)
                if isinstance(item.get("input_artifact_hashes"), dict)
                else {}
            )
            sha256 = snapshot.get("sha256") if isinstance(snapshot, dict) else None
            lines.append(f"- {artifact} (sha256={sha256})")
        lines.extend(
            [
                "",
                "Prerequisite artifacts:",
            ]
        )
        prerequisites = item.get("prerequisite_artifacts") or []
        if not prerequisites:
            lines.append("- none")
        for artifact in prerequisites:
            lines.append(f"- {artifact}")
        lines.append("")
        lines.append("Prerequisite commands:")
        prerequisite_commands = item.get("prerequisite_commands") or []
        if not prerequisite_commands:
            lines.append("- none")
        for command in prerequisite_commands:
            lines.append(f"- `{command}`")
        lines.extend(
            [
                "",
                "Expected artifacts:",
            ]
        )
        artifacts = item.get("expected_artifacts") or []
        if not artifacts:
            lines.append("- none")
        for artifact in artifacts:
            lines.append(f"- {artifact}")
        lines.append("")
        lines.append("Acceptance checks:")
        for check in item.get("acceptance_checks") or []:
            lines.append(f"- {check}")
        lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _markdown_guarded_command(item: dict[str, Any]) -> str:
    command = str(item.get("command") or "")
    preflight = item.get("preflight") if isinstance(item.get("preflight"), dict) else {}
    if _is_guarded_qualification_api_command(command):
        safety_violations = preflight.get("safety_violations")
        if (
            preflight.get("state") == "blocked_safety"
            or preflight.get("preflight_ok") is False
            or preflight.get("qualification_retry_allowed_now") is False
            or (isinstance(safety_violations, list) and bool(safety_violations))
        ):
            return "disabled_until_guard_allows_api_call"
    return command


def _matrix_row_label(row: dict[str, Any], index: int) -> str:
    return str(row.get("course_name") or row.get("rank") or index)


def _has_code_or_label(value: Any) -> bool:
    return isinstance(value, dict) and bool(value.get("code") or value.get("label"))


REQUIRED_GUIDE_TRACE_CODES = {
    "job_scope",
    "task_ksa",
    "course_link",
    "required_optional",
    "level_delivery",
    "human_review",
}
REQUIRED_GUIDE_WORKFLOW_STAGE_CODES = {"C1-1", "C1-2", "C2-1", "C2-2"}


def _missing_guide_trace_fields(payload: dict[str, Any]) -> list[str]:
    trace = payload.get("training_system_guide_trace")
    if not isinstance(trace, dict):
        return ["training_system_guide_trace"]
    missing: list[str] = []
    if trace.get("schema") != "aihr_training_system_guide_trace_v1":
        missing.append("training_system_guide_trace.schema")
    for field in (
        "rubric_source",
        "rubric_role",
        "non_source_data_policy",
        "matrix_reconstruction_fields",
        "guide_workflow_stages",
    ):
        if field not in trace:
            missing.append(f"training_system_guide_trace.{field}")
    stages = trace.get("guide_workflow_stages")
    if not isinstance(stages, list) or not stages:
        workflow = trace.get("guide_workflow") if isinstance(trace.get("guide_workflow"), dict) else {}
        stages = workflow.get("steps") if isinstance(workflow.get("steps"), list) else []
    if not stages:
        missing.append("training_system_guide_trace.guide_workflow_stages")
    else:
        stage_codes = {
            str(item.get("code"))
            for item in stages
            if isinstance(item, dict) and item.get("code")
        }
        for code in sorted(REQUIRED_GUIDE_WORKFLOW_STAGE_CODES - stage_codes):
            missing.append(f"training_system_guide_trace.guide_workflow_stages.{code}")
        for index, item in enumerate(stages, start=1):
            if not isinstance(item, dict):
                missing.append(f"training_system_guide_trace.guide_workflow_stages.row_{index}")
                continue
            for field in ("code", "title", "status", "evidence"):
                if field not in item:
                    missing.append(f"training_system_guide_trace.guide_workflow_stages.row_{index}.{field}")
            if item.get("status") not in {"ready", "needs_review"}:
                missing.append(
                    f"training_system_guide_trace.guide_workflow_stages.row_{index}.status:{item.get('status')}"
                )
    checks = trace.get("checks")
    if not isinstance(checks, list) or not checks:
        missing.append("training_system_guide_trace.checks")
        return missing
    codes = {
        str(item.get("code"))
        for item in checks
        if isinstance(item, dict) and item.get("code")
    }
    for code in sorted(REQUIRED_GUIDE_TRACE_CODES - codes):
        missing.append(f"training_system_guide_trace.checks.{code}")
    for index, item in enumerate(checks, start=1):
        if not isinstance(item, dict):
            missing.append(f"training_system_guide_trace.checks.row_{index}")
            continue
        for field in ("code", "label", "status", "evidence"):
            if field not in item:
                missing.append(f"training_system_guide_trace.checks.row_{index}.{field}")
        if item.get("status") not in {"ready", "needs_review"}:
            missing.append(f"training_system_guide_trace.checks.row_{index}.status:{item.get('status')}")
    return missing


def _public_json_contract_checks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    matrix = payload.get("training_system_matrix") or []
    summary = payload.get("training_system_summary") or {}
    missing_guide_trace_fields = _missing_guide_trace_fields(payload)
    recommended_path = payload.get("recommended_path") if isinstance(payload.get("recommended_path"), list) else []
    missing_recommended_path_fields = _missing_recommended_path_fields(payload)
    missing_scope_baseline_fields = _missing_scope_baseline_fields(payload)
    missing_course_intake_requirements_fields = _missing_course_intake_requirements_fields(payload)
    missing_training_course_inventory_template_fields = _missing_training_course_inventory_template_fields(payload)
    missing_training_necessity_review_fields = _missing_training_necessity_review_fields(payload)
    missing_annual_operation_plan_fields = _missing_annual_operation_plan_fields(payload)
    missing_query_route_fields = _missing_query_route_fields(payload)
    course_count = summary.get("course_count")
    missing_need: list[str] = []
    missing_evidence: list[str] = []
    missing_planner_fields: list[str] = []
    required_planner_fields = {
        "job_scope",
        "target_level_band",
        "education_type",
        "required_optional_basis",
        "delivery_operation",
        "planner_grouping",
        "task_ksa_basis",
        "course_scope_fit",
        "facility_constraint_fit",
        "specificity_warning",
        "duplicate_or_generic_warning",
        "mapping_strength",
        "mapping_strength_warning",
        "decision_state",
        "evidence_chain",
        "human_review",
    }
    required_planner_value_fields = {"required_optional"}
    required_task_ksa_fields = {
        "basis_types",
        "target_scope_ksa",
        "gap_ksa",
        "training_goal_ksa",
        "covered_elements",
    }
    required_facility_fit_fields = {
        "status",
        "requested",
        "available",
        "matched",
        "missing",
        "rationale",
    }
    required_human_review_fields = {
        "severity",
        "prompt",
        "action",
        "review_board_hint",
        "flags",
    }
    required_decision_state_fields = {
        "schema",
        "status",
        "decision_required",
        "system_suggestion",
        "allowed_decisions",
        "approval_claim",
        "message",
    }
    required_evidence_chain_fields = {"schema", "chain_order", "links", "completeness", "message"}
    for index, row in enumerate(matrix, start=1):
        if not isinstance(row, dict):
            missing_need.append(str(index))
            missing_evidence.append(str(index))
            missing_planner_fields.append(str(index))
            continue
        label = _matrix_row_label(row, index)
        if not _has_code_or_label(row.get("need_classification")):
            missing_need.append(label)
        if not _has_code_or_label(row.get("evidence_directness")):
            missing_evidence.append(label)
        missing_fields = sorted(field for field in required_planner_fields if not isinstance(row.get(field), dict))
        missing_fields.extend(
            field for field in sorted(required_planner_value_fields) if row.get(field) in (None, "")
        )
        task_ksa_basis = row.get("task_ksa_basis")
        if isinstance(task_ksa_basis, dict):
            missing_fields.extend(
                f"task_ksa_basis.{field}"
                for field in sorted(required_task_ksa_fields - set(task_ksa_basis))
            )
        facility_fit = row.get("facility_constraint_fit")
        if isinstance(facility_fit, dict):
            missing_fields.extend(
                f"facility_constraint_fit.{field}"
                for field in sorted(required_facility_fit_fields - set(facility_fit))
            )
        human_review = row.get("human_review")
        if isinstance(human_review, dict):
            missing_fields.extend(
                f"human_review.{field}"
                for field in sorted(required_human_review_fields - set(human_review))
            )
        decision_state = row.get("decision_state")
        if isinstance(decision_state, dict):
            missing_fields.extend(
                f"decision_state.{field}"
                for field in sorted(required_decision_state_fields - set(decision_state))
            )
            if decision_state.get("schema") != "aihr_training_row_decision_state_v1":
                missing_fields.append(f"decision_state.schema:{decision_state.get('schema')}")
            if decision_state.get("status") != "pending_human_decision":
                missing_fields.append(f"decision_state.status:{decision_state.get('status')}")
            if decision_state.get("approval_claim") is not False:
                missing_fields.append("decision_state.approval_claim")
        evidence_chain = row.get("evidence_chain")
        if isinstance(evidence_chain, dict):
            missing_fields.extend(
                f"evidence_chain.{field}"
                for field in sorted(required_evidence_chain_fields - set(evidence_chain))
            )
            if evidence_chain.get("schema") != "aihr_course_evidence_chain_v1":
                missing_fields.append(f"evidence_chain.schema:{evidence_chain.get('schema')}")
            if not isinstance(evidence_chain.get("links"), list) or not evidence_chain.get("links"):
                missing_fields.append("evidence_chain.links")
        if missing_fields:
            missing_planner_fields.append(f"{label}: {', '.join(missing_fields)}")

    summary_count_ok = isinstance(course_count, int) and course_count == len(matrix)
    return [
        {
            "name": "Public demo schema",
            "ok": payload.get("public_demo_schema") == "aihr_public_demo_v1",
            "detail": str(payload.get("public_demo_schema")),
        },
        {
            "name": "Summary course count",
            "ok": summary_count_ok,
            "detail": f"summary={course_count}, matrix_rows={len(matrix)}",
        },
        {
            "name": "Need classification",
            "ok": bool(matrix) and not missing_need,
            "detail": "all rows" if not missing_need else ", ".join(missing_need[:10]),
        },
        {
            "name": "Evidence directness",
            "ok": bool(matrix) and not missing_evidence,
            "detail": "all rows" if not missing_evidence else ", ".join(missing_evidence[:10]),
        },
        {
            "name": "Query route",
            "ok": not missing_query_route_fields,
            "detail": "route evidence present" if not missing_query_route_fields else ", ".join(missing_query_route_fields[:10]),
        },
        {
            "name": "Recommended path",
            "ok": not missing_recommended_path_fields,
            "detail": (
                f"stages={len(recommended_path)}"
                if not missing_recommended_path_fields
                else ", ".join(missing_recommended_path_fields[:10])
            ),
        },
        {
            "name": "Scope baseline",
            "ok": not missing_scope_baseline_fields,
            "detail": (
                "job/NCS scope baseline present"
                if not missing_scope_baseline_fields
                else ", ".join(missing_scope_baseline_fields[:10])
            ),
        },
        {
            "name": "Course intake requirements",
            "ok": not missing_course_intake_requirements_fields,
            "detail": (
                "C1-1 course-investigation intake contract present"
                if not missing_course_intake_requirements_fields
                else ", ".join(missing_course_intake_requirements_fields[:10])
            ),
        },
        {
            "name": "Training course inventory template",
            "ok": not missing_training_course_inventory_template_fields,
            "detail": (
                "C1-1 inventory-table template present"
                if not missing_training_course_inventory_template_fields
                else ", ".join(missing_training_course_inventory_template_fields[:10])
            ),
        },
        {
            "name": "Training necessity review",
            "ok": not missing_training_necessity_review_fields,
            "detail": (
                "C1-2 necessity-review contract present"
                if not missing_training_necessity_review_fields
                else ", ".join(missing_training_necessity_review_fields[:10])
            ),
        },
        {
            "name": "Annual operation plan seed",
            "ok": not missing_annual_operation_plan_fields,
            "detail": (
                "C2-2 operation-plan seed present"
                if not missing_annual_operation_plan_fields
                else ", ".join(missing_annual_operation_plan_fields[:10])
            ),
        },
        {
            "name": "Planner matrix fields",
            "ok": bool(matrix) and not missing_planner_fields,
            "detail": "all rows" if not missing_planner_fields else "; ".join(missing_planner_fields[:10]),
        },
        {
            "name": "Training system guide trace",
            "ok": not missing_guide_trace_fields,
            "detail": "complete" if not missing_guide_trace_fields else ", ".join(missing_guide_trace_fields[:10]),
        },
    ]


def build_mcp_contract_checks(contract: dict[str, Any]) -> list[dict[str, Any]]:
    surface = contract.get("surface") or {}
    tools = contract.get("tools") or []
    tool_names = {str(tool.get("name")) for tool in tools if isinstance(tool, dict)}
    operator_tools = set(contract.get("operator_tools_available") or [])
    advanced_tools = set(contract.get("advanced_tools_available") or [])
    active_tool_count = int(surface.get("active_tool_count") or 0)
    operator_tool_count = int(surface.get("operator_tool_count") or 0)
    missing_public = sorted(REQUIRED_PUBLIC_TOOLS - tool_names)
    router = contract.get("query_router") or {}
    scenarios = router.get("scenarios") or []
    scenario_by_name = {
        str(item.get("scenario")): item
        for item in scenarios
        if isinstance(item, dict)
    }
    missing_scenarios = sorted(set(REQUIRED_QUERY_ROUTER_SCENARIOS) - set(scenario_by_name))
    scenario_failures: list[str] = []
    route_integrity_failures: list[str] = []
    known_tools = tool_names | operator_tools | advanced_tools
    if router.get("schema") != "ncs_query_route_v1":
        route_integrity_failures.append(f"schema={router.get('schema') or 'missing'}")
    if router.get("fingerprint_version") != "route-fingerprint-v1":
        route_integrity_failures.append(
            f"fingerprint_version={router.get('fingerprint_version') or 'missing'}"
        )
    for scenario, expected in REQUIRED_QUERY_ROUTER_SCENARIOS.items():
        item = scenario_by_name.get(scenario)
        if not item:
            continue
        tool = str(item.get("tool") or "")
        required_params = set(item.get("required_params") or [])
        pipeline = {str(step) for step in item.get("pipeline") or []}
        expected_tool_chain = [str(step) for step in item.get("expected_tool_chain") or []]
        expected_params = set(expected["required_params"])
        if tool != expected["tool"]:
            scenario_failures.append(f"{scenario}: tool={tool or 'missing'}")
        if not expected_params.issubset(required_params):
            missing_params = sorted(expected_params - required_params)
            scenario_failures.append(f"{scenario}: missing_params={','.join(missing_params)}")
        if scenario == "operator_review":
            if item.get("requires_operator_surface") is not True:
                route_integrity_failures.append("operator_review: requires_operator_surface_missing")
            if item.get("public_executable") is True and not operator_tool_count:
                route_integrity_failures.append("operator_review: public_executable_without_operator_surface")
        unknown_chain_tools = sorted(({tool} | pipeline) - known_tools)
        if unknown_chain_tools:
            scenario_failures.append(f"{scenario}: unknown_tools={','.join(unknown_chain_tools)}")
        if tool and (not expected_tool_chain or expected_tool_chain[0] != tool):
            route_integrity_failures.append(f"{scenario}: primary_tool_not_first")
        missing_chain_tools = sorted(({tool} | pipeline) - set(expected_tool_chain))
        if missing_chain_tools:
            route_integrity_failures.append(
                f"{scenario}: missing_expected_tool_chain={','.join(missing_chain_tools)}"
            )

    return [
        {
            "name": "Public tool count matches tools list",
            "ok": active_tool_count == len(tools) and active_tool_count > 0,
            "detail": f"surface={active_tool_count}, tools={len(tools)}",
        },
        {
            "name": "Required public tools",
            "ok": not missing_public,
            "detail": "all required tools" if not missing_public else ", ".join(missing_public),
        },
        {
            "name": "Operator tools hidden",
            "ok": operator_tool_count == 0,
            "detail": f"operator_tool_count={operator_tool_count}",
        },
        {
            "name": "Query router present",
            "ok": bool(router) and bool(scenarios),
            "detail": f"scenario_count={router.get('scenario_count')}",
        },
        {
            "name": "Required query scenarios",
            "ok": not missing_scenarios,
            "detail": "all required scenarios" if not missing_scenarios else ", ".join(missing_scenarios),
        },
        {
            "name": "Query scenario contracts",
            "ok": not scenario_failures,
            "detail": "all scenario contracts" if not scenario_failures else "; ".join(scenario_failures[:10]),
        },
        {
            "name": "Query route integrity contract",
            "ok": not route_integrity_failures,
            "detail": "route integrity contract present"
            if not route_integrity_failures
            else "; ".join(route_integrity_failures[:10]),
        },
    ]


def build_aihr_demo_contract(
    json_paths: list[Path],
    html_path: Path | None = None,
) -> dict[str, Any] | None:
    if not json_paths and html_path is None:
        return None

    failures: list[dict[str, Any]] = []
    json_artifacts: list[dict[str, Any]] = []
    for path in json_paths:
        artifact: dict[str, Any] = {"path": str(path)}
        if not path.exists():
            artifact.update({"ok": False, "error": "missing_json_artifact"})
            failures.append({"path": str(path), "check": "json_exists", "detail": "missing"})
            json_artifacts.append(artifact)
            continue
        try:
            raw_text = path.read_text(encoding="utf-8-sig")
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            artifact.update({"ok": False, "error": "invalid_json", "detail": str(exc)})
            failures.append({"path": str(path), "check": "json_parse", "detail": str(exc)})
            json_artifacts.append(artifact)
            continue

        public_metadata_markers = _public_metadata_key_markers(payload)
        checks = [
            {"name": label, "ok": bool(ok), "detail": detail}
            for label, ok, detail in _contract_checks(payload)
        ]
        checks.extend(_public_json_contract_checks(payload))
        checks.append(
            {
                "name": "Public metadata redacted",
                "ok": not public_metadata_markers,
                "detail": "redacted" if not public_metadata_markers else ", ".join(public_metadata_markers),
            }
        )
        artifact.update(
            {
                "ok": all(check["ok"] for check in checks),
                "view": payload.get("view"),
                "matrix_rows": len(payload.get("training_system_matrix") or []),
                "course_count": (payload.get("training_system_summary") or {}).get("course_count"),
                "checks": checks,
            }
        )
        for check in checks:
            if not check["ok"]:
                failures.append({"path": str(path), "check": check["name"], "detail": check["detail"]})
        json_artifacts.append(artifact)

    html_artifact: dict[str, Any] | None = None
    if html_path is not None:
        html_artifact = {"path": str(html_path)}
        if not html_path.exists():
            html_artifact.update({"ok": False, "error": "missing_html_artifact"})
            failures.append({"path": str(html_path), "check": "html_exists", "detail": "missing"})
        else:
            text = html_path.read_text(encoding="utf-8")
            leaked_markers = [
                marker
                for marker in SENSITIVE_DEMO_MARKERS
                if marker.lower() in text.lower()
            ]
            leaked_internal_markers = [
                marker
                for marker in sorted(PUBLIC_DEMO_STRIP_KEYS)
                if marker not in PUBLIC_DEMO_SCHEMA_VALUE_MARKERS and marker.lower() in text.lower()
            ]
            html_checks = [
                {
                    "name": "Korean title",
                    "ok": "AI-HR 교육훈련체계 데모" in text,
                    "detail": "AI-HR 교육훈련체계 데모",
                },
                {
                    "name": "Compatibility title",
                    "ok": "AI-HR Education Plan Demo" in text,
                    "detail": "AI-HR Education Plan Demo",
                },
                {
                    "name": "Training-System Summary",
                    "ok": "Training-System Summary" in text,
                    "detail": "present" if "Training-System Summary" in text else "missing",
                },
                {
                    "name": "Training-System Matrix",
                    "ok": "Training-System Matrix" in text,
                    "detail": "present" if "Training-System Matrix" in text else "missing",
                },
                {
                    "name": "2026 Guide Trace",
                    "ok": "2026 Guide Trace" in text,
                    "detail": "present" if "2026 Guide Trace" in text else "missing",
                },
                {
                    "name": "Recommended Path",
                    "ok": "Recommended Path" in text,
                    "detail": "present" if "Recommended Path" in text else "missing",
                },
                {
                    "name": "Scope Baseline",
                    "ok": "Scope Baseline" in text and "scope_baseline" in text,
                    "detail": "present" if "Scope Baseline" in text and "scope_baseline" in text else "missing",
                },
                {
                    "name": "Course intake requirements",
                    "ok": (
                        "Course Intake Requirements" in text
                        and "course_intake_requirements" in text
                        and "aihr_course_intake_requirements_v1" in text
                    ),
                    "detail": (
                        "present"
                        if "Course Intake Requirements" in text
                        and "course_intake_requirements" in text
                        and "aihr_course_intake_requirements_v1" in text
                        else "missing"
                    ),
                },
                {
                    "name": "Training course inventory template",
                    "ok": (
                        "Training Course Inventory Template" in text
                        and "training_course_inventory_template" in text
                        and "aihr_training_course_inventory_template_v1" in text
                    ),
                    "detail": (
                        "present"
                        if "Training Course Inventory Template" in text
                        and "training_course_inventory_template" in text
                        and "aihr_training_course_inventory_template_v1" in text
                        else "missing"
                    ),
                },
                {
                    "name": "Training necessity review",
                    "ok": (
                        "Training Necessity Review" in text
                        and "training_necessity_review" in text
                        and "aihr_training_necessity_review_v1" in text
                    ),
                    "detail": (
                        "present"
                        if "Training Necessity Review" in text
                        and "training_necessity_review" in text
                        and "aihr_training_necessity_review_v1" in text
                        else "missing"
                    ),
                },
                {
                    "name": "Annual operation plan seed",
                    "ok": "Annual Operation Plan Seed" in text and "aihr_annual_operation_plan_seed_v1" in text,
                    "detail": (
                        "present"
                        if "Annual Operation Plan Seed" in text
                        and "aihr_annual_operation_plan_seed_v1" in text
                        else "missing"
                    ),
                },
                {
                    "name": "Task/KSA Basis",
                    "ok": "Task/KSA Basis" in text,
                    "detail": "present" if "Task/KSA Basis" in text else "missing",
                },
                {
                    "name": "Evidence chain contract",
                    "ok": "evidence_chain" in text and "aihr_course_evidence_chain_v1" in text,
                    "detail": (
                        "present"
                        if "evidence_chain" in text and "aihr_course_evidence_chain_v1" in text
                        else "missing"
                    ),
                },
                {
                    "name": "Mapping strength",
                    "ok": "mapping_strength" in text,
                    "detail": "present" if "mapping_strength" in text else "missing",
                },
                {
                    "name": "Mapping strength warning",
                    "ok": "mapping_strength_warning" in text,
                    "detail": "present" if "mapping_strength_warning" in text else "missing",
                },
                {
                    "name": "Decision state contract",
                    "ok": "decision_state" in text and "pending_human_decision" in text,
                    "detail": (
                        "present"
                        if "decision_state" in text and "pending_human_decision" in text
                        else "missing"
                    ),
                },
                {
                    "name": "Facility fit contract",
                    "ok": "facility_constraint_fit" in text,
                    "detail": "present" if "facility_constraint_fit" in text else "missing",
                },
                {
                    "name": "Human review contract",
                    "ok": "human_review" in text,
                    "detail": "present" if "human_review" in text else "missing",
                },
                {
                    "name": "No sensitive payload markers",
                    "ok": not leaked_markers,
                    "detail": "hidden" if not leaked_markers else ", ".join(leaked_markers),
                },
                {
                    "name": "Public metadata redacted",
                    "ok": not leaked_internal_markers,
                    "detail": "redacted" if not leaked_internal_markers else ", ".join(leaked_internal_markers),
                },
                {
                    "name": "No failing contract chips",
                    "ok": "CHECK" not in text,
                    "detail": "none" if "CHECK" not in text else "CHECK present",
                },
            ]
            html_artifact.update(
                {
                    "ok": all(check["ok"] for check in html_checks),
                    "length": len(text),
                    "checks": html_checks,
                }
            )
            for check in html_checks:
                if not check["ok"]:
                    failures.append({"path": str(html_path), "check": check["name"], "detail": check["detail"]})

    return {
        "ok": not failures,
        "json_artifacts": json_artifacts,
        "html_artifact": html_artifact,
        "failure_count": len(failures),
        "failures": failures,
    }


def _is_pending_release_readiness_artifact(
    item: dict[str, Any],
    pending_release_readiness_path: Path | None,
) -> bool:
    if pending_release_readiness_path is None:
        return False
    if item.get("name") != "readiness_json":
        return False
    return _queue_source_path_matches(item.get("path"), pending_release_readiness_path)


def build_dashboard_surface_contract(
    path: Path | None,
    *,
    pending_release_readiness_path: Path | None = None,
) -> dict[str, Any] | None:
    if path is None:
        return None

    failures: list[dict[str, Any]] = []
    artifact: dict[str, Any] = {"path": str(path)}
    deferred_readiness_snapshot_issues: list[str] = []
    if not path.exists():
        failures.append({"check": "artifact_exists", "detail": "missing"})
        return {
            "ok": False,
            "artifact": artifact | {"ok": False, "error": "missing_dashboard_verification"},
            "failure_count": len(failures),
            "failures": failures,
        }
    try:
        stat = path.stat()
        artifact["size_bytes"] = stat.st_size
        artifact["mtime_utc"] = datetime.fromtimestamp(
            stat.st_mtime,
            timezone.utc,
        ).isoformat(timespec="seconds")
        artifact["content_sha256"] = _current_static_artifact_sha256(path)
    except OSError as exc:
        failures.append({"check": "artifact_stat", "detail": type(exc).__name__})
        return {
            "ok": False,
            "artifact": artifact
            | {
                "ok": False,
                "error": "dashboard_verification_unreadable",
                "detail": type(exc).__name__,
            },
            "failure_count": len(failures),
            "failures": failures,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        failures.append({"check": "json_parse", "detail": str(exc)})
        return {
            "ok": False,
            "artifact": artifact | {"ok": False, "error": "invalid_json", "detail": str(exc)},
            "failure_count": len(failures),
            "failures": failures,
        }
    if not isinstance(payload, dict):
        failures.append({"check": "json_object", "detail": "not an object"})
        return {
            "ok": False,
            "artifact": artifact | {"ok": False, "error": "not_json_object"},
            "failure_count": len(failures),
            "failures": failures,
        }

    checks = [
        {
            "name": "Dashboard verification schema",
            "ok": payload.get("schema") == "aihr_dashboard_surface_verification_v1",
            "detail": str(payload.get("schema")),
        },
        {
            "name": "Dashboard verification ok",
            "ok": payload.get("ok") is True,
            "detail": str(payload.get("ok")),
        },
    ]
    endpoint_checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
    failed_endpoint_checks = [
        str(item.get("name") or "unnamed")
        for item in endpoint_checks
        if isinstance(item, dict) and not item.get("ok")
    ]
    endpoint_check_names = {
        str(item.get("name"))
        for item in endpoint_checks
        if isinstance(item, dict) and item.get("name")
    }
    missing_endpoint_checks = sorted(REQUIRED_DASHBOARD_CHECK_NAMES - endpoint_check_names)
    human_gated_execution = []
    unsafe_manual_items = []
    raw_queue_status_auto_start_violations: list[str] = []
    queue_status_api_check: dict[str, Any] = {}
    queue_run_api_check: dict[str, Any] = {}
    live_queue_status_source_queue_path = None
    live_queue_run_source_queue_path = None
    review_chain_safety_check: dict[str, Any] = {}
    ksa_definitions_api_check: dict[str, Any] = {}
    for item in endpoint_checks:
        if isinstance(item, dict) and item.get("name") == "queue_status_api":
            queue_status_api_check = item
            if item.get("source_queue_path"):
                live_queue_status_source_queue_path = str(item.get("source_queue_path"))
            human_gated_execution = [
                str(value)
                for value in (item.get("human_gated_execution") or [])
                if str(value).strip()
            ]
            unsafe_manual_items = [
                str(value)
                for value in (item.get("unsafe_manual_items") or [])
                if str(value).strip()
            ]
            raw_queue_status_auto_start_violations.extend(
                _queue_status_raw_auto_start_violations(item)
            )
        if isinstance(item, dict) and item.get("name") == "agent_queue_run_api":
            queue_run_api_check = item
            if item.get("source_queue_path"):
                live_queue_run_source_queue_path = str(item.get("source_queue_path"))
        if isinstance(item, dict) and item.get("name") == "review_chain_safety":
            review_chain_safety_check = item
        if isinstance(item, dict) and item.get("name") == "ksa_definitions_api":
            ksa_definitions_api_check = item
    checks.append(
        {
            "name": "Endpoint checks",
            "ok": bool(endpoint_checks) and not failed_endpoint_checks and not missing_endpoint_checks,
            "detail": (
                f"count={len(endpoint_checks)}"
                if not failed_endpoint_checks and not missing_endpoint_checks
                else "failed="
                + ",".join(failed_endpoint_checks[:10])
                + "; missing="
                + ",".join(missing_endpoint_checks[:10])
            ),
        }
    )
    queue_summary = payload.get("queue_status_summary") if isinstance(payload.get("queue_status_summary"), dict) else {}
    checks.append(
        {
            "name": "Queue status is guarded",
            "ok": bool(queue_status_api_check) and not human_gated_execution and not unsafe_manual_items,
            "detail": f"blocked_count={queue_summary.get('blocked_count')}",
        }
    )
    checks.append(
        {
            "name": "No unsafe human-gated execution items",
            "ok": not human_gated_execution and not unsafe_manual_items,
            "detail": (
                "none"
                if not human_gated_execution and not unsafe_manual_items
                else (
                    "human_gated="
                    + ",".join(human_gated_execution[:10])
                    + "; unsafe_manual="
                    + ",".join(unsafe_manual_items[:10])
                )
            ),
        }
    )
    queue_status_summary_from_api = (
        queue_status_api_check.get("summary")
        if isinstance(queue_status_api_check.get("summary"), dict)
        else {}
    )
    checks.append(
        {
            "name": "Manual queue guardrails",
            "ok": (
                bool(queue_status_api_check)
                and queue_status_api_check.get("ok") is True
                and isinstance(queue_status_api_check.get("guarded_manual_items"), list)
                and not unsafe_manual_items
                and not human_gated_execution
                and "state_counts" in queue_status_summary_from_api
            ),
            "detail": (
                "guarded_manual="
                + str(queue_status_api_check.get("guarded_manual_items"))
                + ", unsafe_manual="
                + str(unsafe_manual_items)
            ),
        }
    )
    queue_run_output_issues = queue_run_api_check.get("output_issues")
    queue_run_api_summary = (
        queue_run_api_check.get("summary")
        if isinstance(queue_run_api_check.get("summary"), dict)
        else {}
    )
    queue_run_api_failure_issues = _queue_run_failure_issues_from_summary_and_statuses(
        queue_run_api_summary,
        queue_run_api_check.get("run_statuses")
        if isinstance(queue_run_api_check.get("run_statuses"), list)
        else None,
    )
    checks.append(
        {
            "name": "Queue run API actual execution evidence",
            "ok": (
                bool(queue_run_api_check)
                and queue_run_api_check.get("actual_run") is True
                and queue_run_api_check.get("output_tails_suppressed") is True
                and isinstance(queue_run_output_issues, list)
                and not queue_run_output_issues
                and not queue_run_api_failure_issues
            ),
            "detail": (
                "actual_run="
                + str(queue_run_api_check.get("actual_run"))
                + ", output_tails_suppressed="
                + str(queue_run_api_check.get("output_tails_suppressed"))
                + ", output_issues="
                + str(queue_run_output_issues)
                + ", run_failure_issues="
                + str(queue_run_api_failure_issues)
            ),
        }
    )
    top_level_ksa_definition_summary = (
        payload.get("ksa_definition_summary")
        if isinstance(payload.get("ksa_definition_summary"), dict)
        else {}
    )
    endpoint_ksa_definition_summary = (
        ksa_definitions_api_check.get("ksa_definition_summary")
        if isinstance(ksa_definitions_api_check.get("ksa_definition_summary"), dict)
        else {}
    )
    ksa_definition_summary = endpoint_ksa_definition_summary or top_level_ksa_definition_summary
    ksa_summary_mismatches = []
    if endpoint_ksa_definition_summary and top_level_ksa_definition_summary:
        for field in (
            "schema",
            "item_count",
            "matching_ksa",
            "status_update_allowed",
            "raw_ksa_preserved",
            "raw_to_label_visible",
            "source_provenance_visible",
            "missing_item_fields",
        ):
            if endpoint_ksa_definition_summary.get(field) != top_level_ksa_definition_summary.get(field):
                ksa_summary_mismatches.append(field)
    ksa_missing_item_fields = (
        ksa_definition_summary.get("missing_item_fields")
        if isinstance(ksa_definition_summary.get("missing_item_fields"), list)
        else []
    )
    checks.append(
        {
            "name": "KSA definition dashboard contract",
            "ok": (
                bool(ksa_definitions_api_check)
                and ksa_definitions_api_check.get("ok") is True
                and ksa_definition_summary.get("schema") == "ncs_ksa_definition_dashboard_v1"
                and (_safe_int(ksa_definition_summary.get("item_count")) or 0) > 0
                and (_safe_int(ksa_definition_summary.get("matching_ksa")) or 0) > 0
                and ksa_definition_summary.get("status_update_allowed") is False
                and ksa_definition_summary.get("raw_ksa_preserved") is True
                and ksa_definition_summary.get("raw_to_label_visible") is True
                and ksa_definition_summary.get("source_provenance_visible") is True
                and not ksa_missing_item_fields
                and not ksa_summary_mismatches
            ),
            "detail": (
                "raw_to_label_visible=True"
                if (
                    bool(ksa_definitions_api_check)
                    and ksa_definition_summary.get("raw_to_label_visible") is True
                    and ksa_definition_summary.get("source_provenance_visible") is True
                    and not ksa_missing_item_fields
                    and not ksa_summary_mismatches
                )
                else (
                    f"endpoint_ok={ksa_definitions_api_check.get('ok')}, "
                    f"schema={ksa_definition_summary.get('schema')}, "
                    f"item_count={ksa_definition_summary.get('item_count')}, "
                    f"status_update_allowed={ksa_definition_summary.get('status_update_allowed')}, "
                    f"raw_ksa_preserved={ksa_definition_summary.get('raw_ksa_preserved')}, "
                    f"raw_to_label_visible={ksa_definition_summary.get('raw_to_label_visible')}, "
                    f"source_provenance_visible={ksa_definition_summary.get('source_provenance_visible')}, "
                    f"missing_item_fields={ksa_missing_item_fields}, "
                    f"summary_mismatches={ksa_summary_mismatches}"
                )
            ),
        }
    )
    top_level_review_chain_summary = (
        payload.get("review_chain_safety_summary")
        if isinstance(payload.get("review_chain_safety_summary"), dict)
        else {}
    )
    endpoint_review_chain_summary = (
        review_chain_safety_check.get("review_chain_safety_summary")
        if isinstance(review_chain_safety_check.get("review_chain_safety_summary"), dict)
        else {}
    )
    review_chain_summary = endpoint_review_chain_summary or top_level_review_chain_summary
    review_chain_summary_mismatches = []
    if endpoint_review_chain_summary and top_level_review_chain_summary:
        for field in (
            "schema",
            "contract_ok",
            "approval_claim",
            "db_writes",
            "status_update_allowed",
            "source_payload_exposed",
            "do_not_set_human_reviewed_accepted_reviewed_automatically",
            "human_decision_required_for_status_update",
            "review_surface_contract_source",
            "next_request_unit_triage_present",
            "nested_review_surface_contract_present",
            "packet_index_exists",
            "packet_index_non_empty",
            "packet_index_contract_ok",
            "legacy_status_needs_reconfirmation_count",
            "rows_without_packet_backed_provenance",
            "blocked_automation_actions",
            "missing_blocked_automation_actions",
            "sensitive_markers",
            "issues",
            "learning_module_visible_items",
            "ncs_report_visible_items",
            "ocr_context_card_count",
        ):
            if endpoint_review_chain_summary.get(field) != top_level_review_chain_summary.get(field):
                review_chain_summary_mismatches.append(field)
    blocked_actions = (
        review_chain_summary.get("blocked_automation_actions")
        if isinstance(review_chain_summary.get("blocked_automation_actions"), list)
        else []
    )
    missing_blocked_actions = sorted(REQUIRED_REVIEW_CHAIN_BLOCKED_ACTIONS - set(blocked_actions))
    review_chain_issues = (
        review_chain_summary.get("issues")
        if isinstance(review_chain_summary.get("issues"), list)
        else []
    )
    review_chain_provenance_issues: list[str] = []
    review_surface_contract_source = review_chain_summary.get("review_surface_contract_source")
    next_request_unit_triage_present = review_chain_summary.get("next_request_unit_triage_present")
    nested_review_surface_contract_present = review_chain_summary.get(
        "nested_review_surface_contract_present"
    )
    if review_surface_contract_source not in {
        "nested_unit_triage",
        "root_fallback_no_unit_triage",
    }:
        review_chain_provenance_issues.append("review_surface_contract_source_missing_or_invalid")
    if next_request_unit_triage_present is True:
        if nested_review_surface_contract_present is not True:
            review_chain_provenance_issues.append(
                "nested_review_surface_contract_missing_for_unit_triage"
            )
        if review_surface_contract_source != "nested_unit_triage":
            review_chain_provenance_issues.append(
                "review_surface_contract_source_not_nested_for_unit_triage"
            )
    elif next_request_unit_triage_present is False:
        if review_surface_contract_source != "root_fallback_no_unit_triage":
            review_chain_provenance_issues.append(
                "review_surface_contract_source_not_root_fallback_without_unit_triage"
            )
    else:
        review_chain_provenance_issues.append("next_request_unit_triage_presence_missing")
    review_chain_ok = (
        bool(review_chain_summary)
        and review_chain_safety_check.get("ok") is True
        and review_chain_summary.get("contract_ok") is True
        and review_chain_summary.get("schema") == "aihr_plan_review_workflow_handoff_v1"
        and review_chain_summary.get("approval_claim") is False
        and review_chain_summary.get("db_writes") is False
        and review_chain_summary.get("status_update_allowed") is False
        and review_chain_summary.get("source_payload_exposed") is False
        and review_chain_summary.get("do_not_set_human_reviewed_accepted_reviewed_automatically") is True
        and review_chain_summary.get("human_decision_required_for_status_update") is True
        and review_chain_summary.get("packet_index_exists") is True
        and review_chain_summary.get("packet_index_non_empty") is True
        and review_chain_summary.get("packet_index_contract_ok") is True
        and not review_chain_issues
        and not review_chain_provenance_issues
        and not missing_blocked_actions
        and not review_chain_summary_mismatches
    )
    checks.append(
        {
            "name": "Review chain safety contract",
            "ok": review_chain_ok,
            "detail": (
                "contract_ok=True"
                if review_chain_ok
                else (
                    f"summary_present={bool(review_chain_summary)}, "
                    f"endpoint_ok={review_chain_safety_check.get('ok')}, "
                    f"contract_ok={review_chain_summary.get('contract_ok')}, "
                    f"source_payload_exposed={review_chain_summary.get('source_payload_exposed')}, "
                    f"packet_index_exists={review_chain_summary.get('packet_index_exists')}, "
                    f"packet_index_non_empty={review_chain_summary.get('packet_index_non_empty')}, "
                    f"packet_index_contract_ok={review_chain_summary.get('packet_index_contract_ok')}, "
                    f"missing_blocked_actions={missing_blocked_actions}, "
                    f"summary_mismatches={review_chain_summary_mismatches}, "
                    f"provenance_issues={review_chain_provenance_issues}, "
                    f"issues={review_chain_issues}"
                )
            ),
        }
    )
    live_summaries = (
        payload.get("live_plan_summaries")
        if isinstance(payload.get("live_plan_summaries"), list)
        else []
    )
    bad_live_summaries = []
    bad_training_necessity_summaries = []
    bad_annual_operation_summaries = []
    for item in live_summaries:
        if not isinstance(item, dict):
            bad_live_summaries.append("not_object")
            bad_training_necessity_summaries.append("not_object")
            bad_annual_operation_summaries.append("not_object")
            continue
        sensitive_markers = item.get("sensitive_markers")
        expected_chain = item.get("query_route_expected_tool_chain")
        expected_chain_ok = (
            isinstance(expected_chain, list)
            and "plan_ncs_education_path" in expected_chain
            and "recommend_training_transition" in expected_chain
        )
        matrix_rows = _safe_int(item.get("matrix_rows"))
        necessity_summary = (
            item.get("training_necessity_review_summary")
            if isinstance(item.get("training_necessity_review_summary"), dict)
            else {}
        )
        necessity_row_count = _safe_int(necessity_summary.get("row_count"))
        necessity_approval_blocked = _safe_int(necessity_summary.get("approval_blocked_rows"))
        unsafe_approval_claims = _safe_int(
            necessity_summary.get("unsafe_row_approval_claim_count")
        )
        training_necessity_bad = (
            item.get("training_necessity_review_schema") != "aihr_training_necessity_review_v1"
            or necessity_summary.get("schema") != "aihr_training_necessity_review_v1"
            or necessity_summary.get("guide_stage") != "C1-2"
            or necessity_row_count is None
            or necessity_row_count != matrix_rows
            or necessity_approval_blocked is None
            or necessity_summary.get("approval_claim_safe") is not True
            or (unsafe_approval_claims or 0) > 0
        )
        if training_necessity_bad:
            bad_training_necessity_summaries.append(str(item.get("name") or "unnamed"))
        annual_summary = (
            item.get("annual_operation_plan_summary")
            if isinstance(item.get("annual_operation_plan_summary"), dict)
            else {}
        )
        annual_row_count = _safe_int(annual_summary.get("row_count"))
        annual_operation_bad = (
            item.get("annual_operation_plan_schema") != "aihr_annual_operation_plan_seed_v1"
            or annual_summary.get("schema") != "aihr_annual_operation_plan_seed_v1"
            or annual_summary.get("guide_stage") != "C2-2"
            or annual_row_count is None
            or annual_row_count != matrix_rows
            or annual_summary.get("approval_claim_safe") is not True
        )
        if annual_operation_bad:
            bad_annual_operation_summaries.append(str(item.get("name") or "unnamed"))
        if (
            item.get("ok") is not True
            or item.get("schema") != "aihr_live_plan_v1"
            or item.get("run_mode") != "live_no_save"
            or item.get("view") != "ncs_education_plan"
            or matrix_rows is None
            or matrix_rows <= 0
            or training_necessity_bad
            or annual_operation_bad
            or item.get("missing_matrix_fields")
            or item.get("missing_plan_fields")
            or item.get("guide_trace_schema") != "aihr_training_system_guide_trace_v1"
            or item.get("missing_guide_trace_fields")
            or item.get("query_route_schema") != "ncs_query_route_v1"
            or item.get("query_route_tool") != "plan_ncs_education_path"
            or not item.get("query_route_fingerprint")
            or not expected_chain_ok
            or item.get("query_route_contract_schema") != "ncs_query_route_v1"
            or item.get("query_route_contract_route_first") is not True
            or item.get("query_route_contract_primary_tool") != item.get("query_route_tool")
            or item.get("query_route_contract_fingerprint") != item.get("query_route_fingerprint")
            or item.get("missing_query_route_fields")
            or not isinstance(sensitive_markers, list)
            or bool(sensitive_markers)
        ):
            bad_live_summaries.append(str(item.get("name") or "unnamed"))
    checks.append(
        {
            "name": "Live plan scenario contracts",
            "ok": bool(live_summaries) and not bad_live_summaries,
            "detail": (
                f"scenario_count={len(live_summaries)}"
                if not bad_live_summaries
                else ", ".join(bad_live_summaries[:10])
            ),
        }
    )
    checks.append(
        {
            "name": "Training necessity review summaries",
            "ok": bool(live_summaries) and not bad_training_necessity_summaries,
            "detail": (
                "present_and_safe"
                if not bad_training_necessity_summaries
                else ", ".join(bad_training_necessity_summaries[:10])
            ),
        }
    )
    checks.append(
        {
            "name": "Annual operation plan summaries",
            "ok": bool(live_summaries) and not bad_annual_operation_summaries,
            "detail": (
                "present_and_safe"
                if not bad_annual_operation_summaries
                else ", ".join(bad_annual_operation_summaries[:10])
            ),
        }
    )
    scenario_count_value = (
        _safe_int(payload.get("scenario_count"))
        if payload.get("scenario_count") is not None
        else len(live_summaries)
    )
    checks.append(
        {
            "name": "Multiple live scenarios",
            "ok": (scenario_count_value or 0) >= 2,
            "detail": f"scenario_count={payload.get('scenario_count') if payload.get('scenario_count') is not None else len(live_summaries)}",
        }
    )
    static_artifacts = (
        payload.get("static_artifacts")
        if isinstance(payload.get("static_artifacts"), list)
        else []
    )
    static_artifact_names = {
        str(item.get("name"))
        for item in static_artifacts
        if isinstance(item, dict) and item.get("name")
    }
    static_artifacts_by_name = {
        str(item.get("name")): item
        for item in static_artifacts
        if isinstance(item, dict) and item.get("name")
    }
    queue_source_contract_required = all(
        _artifact_requires_source_queue_contract(static_artifacts_by_name.get(name, {}))
        for name in ("queue_status_json", "queue_run_json")
    )
    freshness_hash_skip_names = sorted(
        name
        for name in static_artifact_names
        if name in STATIC_ARTIFACT_FRESHNESS_HASH_SKIP_NAMES
    )
    freshness_hash_skip_reason = {
        "readiness_json": (
            "cycle_aware_release_dashboard_reference; the release JSON can embed "
            "the dashboard proof that also lists the release JSON"
        ),
        "queue_status_json": "rechecked by queue status canonical snapshot validation",
        "queue_run_json": "rechecked by queue run source queue and status snapshot validation",
    }
    stale_static_artifacts = _static_artifact_freshness_issues(static_artifacts)
    missing_static_artifact_names = sorted(REQUIRED_DASHBOARD_STATIC_ARTIFACT_NAMES - static_artifact_names)
    bad_static_artifacts = []
    bad_guide_surface_audits = []
    bad_ontology_education_audits = []
    bad_review_workflow_handoffs = []
    bad_human_review_backlogs = []
    bad_goal_completion_audits = []
    bad_release_readiness_artifacts = []
    bad_queue_status_artifacts = []
    bad_queue_status_raw_auto_start_artifacts = []
    bad_queue_run_artifacts = []
    bad_queue_source_artifacts = []
    bad_live_queue_source_artifacts = []
    bad_queue_source_contract_artifacts = []
    bad_checkpoint_artifacts = []
    bad_role_static_artifacts = []
    bad_local_path_artifacts = []
    review_gated_checkpoint_artifacts = []
    review_gated_checkpoint_details = []
    bad_query_route_contract_audit_artifacts = []
    bad_api_linkage_summary_artifacts = []
    bad_qualification_retry_hygiene_artifacts = []
    bad_qualification_coverage_plan_artifacts = []
    bad_provenance_reconfirmation_artifacts = []
    provenance_reconfirmation_lineage_hashes: dict[str, str] = {}
    static_artifact_dates = _artifact_dates_from_static_artifacts(static_artifacts)
    dashboard_artifact_stamp = _artifact_stamp_from_text(str(path))
    dashboard_artifact_stamp_family = _artifact_stamp_family(
        dashboard_artifact_stamp,
        dashboard_artifact_stamp,
    )
    static_artifact_stamp_families = _artifact_stamp_families_from_static_artifacts(
        static_artifacts,
        expected_stamp=dashboard_artifact_stamp,
    )
    core_static_artifact_dates = _artifact_dates_from_static_artifacts(
        static_artifacts,
        names=DATE_CONSISTENT_DASHBOARD_STATIC_ARTIFACT_NAMES,
    )
    core_static_artifact_stamp_families = _artifact_stamp_families_from_static_artifacts(
        static_artifacts,
        names=DATE_CONSISTENT_DASHBOARD_STATIC_ARTIFACT_NAMES,
        expected_stamp=dashboard_artifact_stamp,
    )
    dashboard_artifact_date = _artifact_date_from_text(str(path))
    mixed_static_artifact_dates = (
        len(static_artifact_dates) > 1
        or (
            dashboard_artifact_date
            and static_artifact_dates
            and static_artifact_dates != [dashboard_artifact_date]
        )
    )
    core_static_artifact_date_mismatch = (
        len(core_static_artifact_dates) > 1
        or (
            dashboard_artifact_date
            and core_static_artifact_dates
            and any(stamp != dashboard_artifact_date for stamp in core_static_artifact_dates)
        )
    )
    core_static_artifact_stamp_family_mismatch = (
        len(core_static_artifact_stamp_families) > 1
        or (
            dashboard_artifact_stamp_family
            and core_static_artifact_stamp_families
            and any(
                family != dashboard_artifact_stamp_family
                for family in core_static_artifact_stamp_families
            )
        )
    )
    release_readiness_queue_path = next(
        (
            item.get("release_readiness", {}).get("agent_work_queue_path")
            for item in static_artifacts
            if isinstance(item, dict)
            and item.get("name") == "readiness_json"
            and isinstance(item.get("release_readiness"), dict)
        ),
        None,
    )
    for item in static_artifacts:
        if not isinstance(item, dict):
            bad_static_artifacts.append("not_object")
            bad_guide_surface_audits.append("not_object")
            bad_ontology_education_audits.append("not_object")
            continue
        size_bytes = _safe_int(item.get("size_bytes"))
        if item.get("exists") is not True or size_bytes is None or size_bytes <= 0:
            bad_static_artifacts.append(str(item.get("name") or item.get("path") or "unnamed"))
        if (
            not _dashboard_static_artifact_role_ok(item.get("name"), item.get("path"))
            or item.get("role_contract_ok") is False
        ):
            bad_role_static_artifacts.append(
                str(item.get("name") or item.get("path") or "unnamed")
            )
        if item.get("local_path_markers"):
            bad_local_path_artifacts.append(str(item.get("path") or item.get("name") or "unnamed"))
        if item.get("name") == "guide_surface_audit_json":
            audit = item.get("guide_surface_audit") if isinstance(item.get("guide_surface_audit"), dict) else {}
            stage_codes = audit.get("guide_stage_codes") if isinstance(audit.get("guide_stage_codes"), dict) else {}
            missing_stage_codes = sorted({"C1-1", "C1-2", "C2-1", "C2-2"} - set(stage_codes))
            if (
                audit.get("schema") != "aihr_guide_surface_audit_v1"
                or audit.get("ok") is not True
                or _safe_int(audit.get("blocker_count")) != 0
                or _safe_int(audit.get("unsafe_approval_claim_artifacts")) != 0
                or audit.get("approval_claim") is not False
                or audit.get("db_writes") is not False
                or audit.get("guide_role") != "framework_reference"
                or bool(audit.get("sensitive_markers"))
                or audit.get("human_decision_required_for_approval") is not True
                or missing_stage_codes
            ):
                bad_guide_surface_audits.append(str(item.get("path") or item.get("name")))
        if item.get("name") == "ontology_transferability_education_audit_json":
            audit = (
                item.get("ontology_transferability_education_audit")
                if isinstance(item.get("ontology_transferability_education_audit"), dict)
                else {}
            )
            stage_counts = (
                audit.get("guide_stage_counts")
                if isinstance(audit.get("guide_stage_counts"), dict)
                else {}
            )
            missing_stage_codes = sorted({"C1-1", "C1-2", "C2-1", "C2-2"} - set(stage_counts))
            rows_requiring_review = _safe_int(audit.get("rows_requiring_human_review"))
            if (
                audit.get("schema") != "ncs_ontology_transferability_education_system_audit_v1"
                or audit.get("ok") is not False
                or audit.get("contract_ok") is not True
                or audit.get("approval_ready") is not False
                or audit.get("status") != "review_required"
                or _safe_int(audit.get("unsafe_review_status_count")) != 0
                or _safe_int(audit.get("invalid_review_status_count")) != 0
                or audit.get("approval_claim") is not False
                or audit.get("db_writes") is not False
                or audit.get("guide_role") != "framework_reference"
                or audit.get("review_gate_status") != "open"
                or audit.get("review_gate_approval_claim") is not False
                or bool(audit.get("sensitive_markers"))
                or missing_stage_codes
                or rows_requiring_review is None
                or rows_requiring_review <= 0
            ):
                bad_ontology_education_audits.append(str(item.get("path") or item.get("name")))
        if item.get("name") == "review_workflow_handoff_json":
            handoff = (
                item.get("review_workflow_handoff")
                if isinstance(item.get("review_workflow_handoff"), dict)
                else {}
            )
            if (
                handoff.get("schema") != "aihr_plan_review_workflow_handoff_v1"
                or handoff.get("contract_ok") is not True
                or handoff.get("source_payload_exposed") is not False
                or handoff.get("packet_index_exists") is not True
                or handoff.get("packet_index_non_empty") is not True
                or handoff.get("packet_index_contract_ok") is not True
                or bool(handoff.get("sensitive_markers"))
                or bool(handoff.get("issues"))
                or handoff.get("review_surface_contract_source")
                not in {"nested_unit_triage", "root_fallback_no_unit_triage"}
                or (
                    handoff.get("next_request_unit_triage_present") is True
                    and (
                        handoff.get("nested_review_surface_contract_present") is not True
                        or handoff.get("review_surface_contract_source") != "nested_unit_triage"
                    )
                )
                or (
                    handoff.get("next_request_unit_triage_present") is False
                    and handoff.get("review_surface_contract_source")
                    != "root_fallback_no_unit_triage"
                )
                or handoff.get("next_request_unit_triage_present") not in {True, False}
            ):
                bad_review_workflow_handoffs.append(str(item.get("path") or item.get("name")))
        if item.get("name") == "human_review_backlog_json":
            backlog = (
                item.get("human_review_backlog")
                if isinstance(item.get("human_review_backlog"), dict)
                else {}
            )
            backlog_hash_revalidation = _human_review_backlog_source_hash_revalidation(
                item
            )
            backlog["source_hash_revalidation_ok"] = backlog_hash_revalidation.get("ok")
            backlog["source_hash_revalidation_checked_count"] = (
                backlog_hash_revalidation.get("checked_count")
            )
            backlog["source_hash_revalidation_mismatch_count"] = (
                backlog_hash_revalidation.get("mismatch_count")
            )
            backlog["source_hash_revalidation_issues"] = (
                backlog_hash_revalidation.get("issues") or []
            )
            definition_packet = (
                backlog.get("ksa_definition_packet")
                if isinstance(backlog.get("ksa_definition_packet"), dict)
                else {}
            )
            review_policy = (
                backlog.get("review_status_policy")
                if isinstance(backlog.get("review_status_policy"), dict)
                else {}
            )
            sidecar_issues = (
                definition_packet.get("sidecar_consistency_issues")
                if isinstance(definition_packet.get("sidecar_consistency_issues"), list)
                else []
            )
            if (
                backlog.get("schema") != "aihr_human_review_backlog_v1"
                or backlog.get("contract_ok") is not True
                or not _review_status_policy_contract_ok(review_policy)
                or backlog.get("all_seedpacks_safe") is not True
                or _safe_int(backlog.get("total_forbidden_true_field_violations")) != 0
                or _safe_int(backlog.get("total_status_update_allowed_violations")) != 0
                or _safe_int(backlog.get("total_missing_status_update_allowed")) != 0
                or _safe_int(backlog.get("total_trusted_status_proposals")) != 0
                or _safe_int(backlog.get("total_seedpack_structure_issues")) != 0
                or backlog.get("source_hash_contract_ok") is not True
                or backlog_hash_revalidation.get("ok") is not True
                or backlog.get("source_release_hash_scope")
                != "cycle_safe_release_readiness"
                or backlog.get("source_release_cycle_safe_hash_present") is not True
                or _safe_int(backlog.get("queue_input_hash_count")) <= 0
                or backlog.get("queue_supporting_report_inputs_present") is not True
                or definition_packet.get("safety_ok") is not True
                or definition_packet.get("source_payload_exposed") is not False
                or definition_packet.get("status_update_allowed") is not False
                or definition_packet.get("db_writes") is not False
                or definition_packet.get("approval_claim") is not False
                or definition_packet.get("trusted_status_write_allowed") is not False
                or definition_packet.get("raw_source_mutation_allowed") is not False
                or definition_packet.get("sidecar_safety_ok") is not True
                or sidecar_issues
            ):
                bad_human_review_backlogs.append(str(item.get("path") or item.get("name")))
        if item.get("name") == "goal_completion_audit_json":
            audit = (
                item.get("goal_completion_audit")
                if isinstance(item.get("goal_completion_audit"), dict)
                else {}
            )
            definition_packet = (
                audit.get("ksa_definition_packet")
                if isinstance(audit.get("ksa_definition_packet"), dict)
                else {}
            )
            review_policy = (
                audit.get("review_status_policy")
                if isinstance(audit.get("review_status_policy"), dict)
                else {}
            )
            sidecar_issues = (
                definition_packet.get("sidecar_consistency_issues")
                if isinstance(definition_packet.get("sidecar_consistency_issues"), list)
                else []
            )
            open_requirement_count = _safe_int(audit.get("open_requirement_count"))
            verified_requirement_count = _safe_int(audit.get("verified_requirement_count"))
            release_ready = audit.get("release_ready")
            if (
                audit.get("schema") != "aihr_goal_completion_audit_v1"
                or audit.get("contract_ok") is not True
                or not isinstance(release_ready, bool)
                or not _review_status_policy_contract_ok(review_policy)
                or audit.get("release_ready_consistent") is not True
                or open_requirement_count is None
                or verified_requirement_count is None
                or release_ready != (open_requirement_count == 0)
                or audit.get("human_review_backlog_all_seedpacks_safe") is not True
                or _safe_int(audit.get("human_review_backlog_forbidden_true_field_violations")) != 0
                or _safe_int(audit.get("human_review_backlog_status_update_allowed_violations")) != 0
                or _safe_int(audit.get("human_review_backlog_missing_status_update_allowed")) != 0
                or _safe_int(audit.get("human_review_backlog_trusted_status_proposals")) != 0
                or _safe_int(audit.get("human_review_backlog_seedpack_structure_issues")) != 0
                or definition_packet.get("safety_ok") is not True
                or definition_packet.get("source_payload_exposed") is not False
                or definition_packet.get("status_update_allowed") is not False
                or definition_packet.get("db_writes") is not False
                or definition_packet.get("approval_claim") is not False
                or definition_packet.get("trusted_status_write_allowed") is not False
                or definition_packet.get("raw_source_mutation_allowed") is not False
                or definition_packet.get("sidecar_safety_ok") is not True
                or sidecar_issues
            ):
                bad_goal_completion_audits.append(str(item.get("path") or item.get("name")))
        if item.get("name") == "readiness_json":
            readiness = (
                item.get("release_readiness")
                if isinstance(item.get("release_readiness"), dict)
                else {}
            )
            defer_self_snapshot = _is_pending_release_readiness_artifact(
                item,
                pending_release_readiness_path,
            )
            readiness_cycle_safe_hash_issue = _readiness_cycle_safe_hash_issue(item)
            if readiness_cycle_safe_hash_issue:
                if defer_self_snapshot:
                    deferred_readiness_snapshot_issues.append(readiness_cycle_safe_hash_issue)
                else:
                    bad_release_readiness_artifacts.append(readiness_cycle_safe_hash_issue)
            readiness_lineage_issue = _readiness_lineage_issue(item, path)
            if readiness_lineage_issue:
                if defer_self_snapshot:
                    deferred_readiness_snapshot_issues.append(readiness_lineage_issue)
                else:
                    bad_release_readiness_artifacts.append(readiness_lineage_issue)
            if (
                readiness.get("schema") != "aihr_release_readiness_v1"
                or readiness.get("contract_ok") is not True
                or not readiness.get("agent_work_queue_path")
            ):
                bad_release_readiness_artifacts.append(str(item.get("path") or item.get("name")))
        if item.get("name") in REQUIRED_DASHBOARD_CHECKPOINT_JSON_ARTIFACT_NAMES:
            checkpoint = (
                item.get("checkpoint")
                if isinstance(item.get("checkpoint"), dict)
                else {}
            )
            if checkpoint.get("contract_ok") is not True:
                review_gated_detail = _dashboard_checkpoint_review_gate_detail(
                    name=str(item.get("name") or ""),
                    path=str(item.get("path") or item.get("name")),
                    checkpoint=checkpoint,
                )
                if review_gated_detail:
                    review_gated_checkpoint_artifacts.append(
                        str(item.get("path") or item.get("name"))
                    )
                    review_gated_checkpoint_details.append(review_gated_detail)
                else:
                    bad_checkpoint_artifacts.append(str(item.get("path") or item.get("name")))
        if item.get("name") == "queue_status_json":
            queue_status = (
                item.get("queue_status")
                if isinstance(item.get("queue_status"), dict)
                else {}
            )
            queue_status_content_hash_issue = _static_artifact_content_hash_issue(item)
            if queue_status_content_hash_issue:
                bad_queue_status_artifacts.append(queue_status_content_hash_issue)
            if not release_readiness_queue_path:
                bad_queue_source_artifacts.append(
                    f"{item.get('path') or item.get('name')}:expected_release_queue_missing"
                )
            elif not queue_status.get("source_queue_path"):
                bad_queue_source_artifacts.append(
                    f"{item.get('path') or item.get('name')}:source_queue_path_missing"
                )
            elif not _queue_source_path_matches(
                queue_status.get("source_queue_path"),
                release_readiness_queue_path,
            ):
                bad_queue_source_artifacts.append(
                    f"{item.get('path') or item.get('name')}:source={queue_status.get('source_queue_path')}"
                )
            raw_auto_start_violations = _queue_status_raw_auto_start_violations(
                queue_status
            )
            raw_queue_status_auto_start_violations.extend(raw_auto_start_violations)
            bad_queue_source_contract_artifacts.extend(
                _queue_source_contract_issues_for_artifact(
                    item,
                    queue_status.get("source_queue_path"),
                    require_source_queue_contract=queue_source_contract_required,
                )
            )
            if (
                queue_status.get("schema") != "aihr_agent_queue_status_v1"
                or queue_status.get("contract_ok") is not True
                or bool(queue_status.get("human_gated_execution"))
                or bool(queue_status.get("unsafe_manual_items"))
            ):
                bad_queue_status_artifacts.append(str(item.get("path") or item.get("name")))
            if raw_auto_start_violations:
                bad_queue_status_raw_auto_start_artifacts.append(
                    str(item.get("path") or item.get("name"))
                )
        if item.get("name") == "queue_run_json":
            queue_run = (
                item.get("queue_run")
                if isinstance(item.get("queue_run"), dict)
                else {}
            )
            queue_run_content_hash_issue = _static_artifact_content_hash_issue(item)
            queue_run_public_sync_issue = _queue_run_public_sync_issue(item)
            queue_run_source_queue_sync_issue = _queue_run_source_queue_sync_issue(item)
            queue_status_snapshot_sync_issue = _queue_status_snapshot_sync_issue(item)
            queue_run_lineage_issues = _queue_run_lineage_issues(item)
            queue_run_failure_issues = list(
                queue_run.get("run_failure_issues")
                if isinstance(queue_run.get("run_failure_issues"), list)
                else []
            )
            queue_run_failure_issues.extend(
                issue
                for issue in _queue_run_failure_issues_from_summary_and_statuses(
                    queue_run,
                    queue_run.get("run_statuses")
                    if isinstance(queue_run.get("run_statuses"), list)
                    else None,
                )
                if issue not in queue_run_failure_issues
            )
            if not release_readiness_queue_path:
                bad_queue_source_artifacts.append(
                    f"{item.get('path') or item.get('name')}:expected_release_queue_missing"
                )
            elif not queue_run.get("source_queue_path"):
                bad_queue_source_artifacts.append(
                    f"{item.get('path') or item.get('name')}:source_queue_path_missing"
                )
            elif not _queue_source_path_matches(
                queue_run.get("source_queue_path"),
                release_readiness_queue_path,
            ):
                bad_queue_source_artifacts.append(
                    f"{item.get('path') or item.get('name')}:source={queue_run.get('source_queue_path')}"
                )
            if queue_run_public_sync_issue:
                bad_queue_run_artifacts.append(queue_run_public_sync_issue)
            if queue_run_content_hash_issue:
                bad_queue_run_artifacts.append(queue_run_content_hash_issue)
            if queue_run_source_queue_sync_issue:
                bad_queue_run_artifacts.append(queue_run_source_queue_sync_issue)
            if queue_status_snapshot_sync_issue:
                bad_queue_run_artifacts.append(queue_status_snapshot_sync_issue)
            bad_queue_run_artifacts.extend(queue_run_lineage_issues)
            bad_queue_source_contract_artifacts.extend(
                _queue_source_contract_issues_for_artifact(
                    item,
                    queue_run.get("source_queue_path"),
                    require_source_queue_contract=queue_source_contract_required,
                )
            )
            if (
                queue_run.get("schema") != "aihr_agent_queue_run_v1"
                or queue_run.get("contract_ok") is not True
                or queue_run.get("actual_run") is not True
                or bool(queue_run.get("output_issues"))
                or bool(queue_run_failure_issues)
            ):
                bad_queue_run_artifacts.append(
                    str(item.get("path") or item.get("name"))
                    + (
                        ":run_failure_issues="
                        + ",".join(queue_run_failure_issues[:5])
                        if queue_run_failure_issues
                        else ""
                    )
                )
        if item.get("name") == "query_route_contract_audit_json":
            audit = (
                item.get("query_route_contract_audit")
                if isinstance(item.get("query_route_contract_audit"), dict)
                else {}
            )
            case_count = audit.get("case_count")
            pass_count = audit.get("pass_count")
            failure_count = audit.get("failure_count")
            row_count = audit.get("row_count")
            failure_summary_count = audit.get("failure_summary_count")
            passed_row_count = audit.get("passed_row_count")
            failed_row_count = audit.get("failed_row_count")
            malformed_row_count = audit.get("malformed_row_count")
            row_issue_count = audit.get("row_issue_count")
            if (
                audit.get("schema") != "ncs_query_route_contract_audit_v1"
                or audit.get("ok") is not True
                or audit.get("status") != "pass"
                or audit.get("status_update_allowed") is not False
                or audit.get("db_writes") is not False
                or audit.get("approval_claim") is not False
                or type(case_count) is not int
                or case_count <= 0
                or type(pass_count) is not int
                or pass_count != case_count
                or type(failure_count) is not int
                or failure_count != 0
                or type(row_count) is not int
                or row_count != case_count
                or type(failure_summary_count) is not int
                or failure_summary_count != 0
                or type(passed_row_count) is not int
                or passed_row_count != case_count
                or type(failed_row_count) is not int
                or failed_row_count != 0
                or type(malformed_row_count) is not int
                or malformed_row_count != 0
                or type(row_issue_count) is not int
                or row_issue_count != 0
                or audit.get("contract_ok") is not True
            ):
                bad_query_route_contract_audit_artifacts.append(
                    str(item.get("path") or item.get("name"))
                )
        if item.get("name") == "api_linkage_summary_json":
            summary = (
                item.get("api_linkage_summary")
                if isinstance(item.get("api_linkage_summary"), dict)
                else {}
            )
            coverage_hint = (
                summary.get("qualification_coverage_plan_hint")
                if isinstance(summary.get("qualification_coverage_plan_hint"), dict)
                else {}
            )
            coverage_scope = coverage_hint.get("scope")
            scope_major_codes = coverage_hint.get("scope_major_codes")
            scope_major_codes_nonempty = bool(scope_major_codes)
            scope_major_codes_bad = scope_major_codes_nonempty or not isinstance(
                scope_major_codes, list
            )
            major_count = _safe_int(summary.get("major_count"))
            hint_total_unit_count = _safe_int(coverage_hint.get("total_unit_count"))
            coverage_hint_bad = (
                coverage_hint.get("coverage_plan_command_scope") != "all_units"
                or coverage_hint.get("must_run_qualification_retry_hygiene_first") is not True
                or coverage_hint.get("guard_required") is not True
                or coverage_hint.get("operator_timing_required") is not True
                or coverage_hint.get("db_writes") is not False
                or coverage_hint.get("api_calls") is not False
                or coverage_hint.get("human_review_status_updates") is not False
                or not isinstance(coverage_hint.get("target_ratio"), (int, float))
                or _safe_int(coverage_hint.get("batch_size")) is None
                or (_safe_int(coverage_hint.get("batch_size")) or 0) <= 0
                or _safe_int(coverage_hint.get("total_unit_count")) is None
                or (_safe_int(coverage_hint.get("total_unit_count")) or 0) <= 0
                or _safe_int(coverage_hint.get("attempted_unit_count")) is None
                or coverage_hint.get("collection_coverage") is None
                or _safe_int(coverage_hint.get("additional_attempted_units_needed")) is None
                or _safe_int(coverage_hint.get("estimated_batch_count")) is None
                or coverage_hint.get("global_coverage_plan_command_present") is not True
                or scope_major_codes_bad
                or (
                    coverage_scope == "all_majors"
                    and (
                        coverage_hint.get("coverage_plan_matches_summary_scope") is not True
                        or coverage_hint.get("coverage_plan_command_present") is not True
                    )
                )
                or coverage_scope != "all_majors"
            )
            if (
                summary.get("schema") != "ncs_api_linkage_summary_v1"
                or summary.get("ok") is not True
                or summary.get("db_writes") is not False
                or summary.get("api_calls") is not False
                or summary.get("human_review_status_updates") is not False
                or summary.get("sqf_active_scoring_source") is not False
                or major_count is None
                or major_count < NCS_FULL_MAJOR_COUNT
                or _safe_int(summary.get("unit_count")) is None
                or (_safe_int(summary.get("unit_count")) or 0) <= 0
                or hint_total_unit_count is None
                or hint_total_unit_count != _safe_int(summary.get("unit_count"))
                or (_safe_int(summary.get("safe_next_action_count")) or 0) <= 0
                or _safe_int(summary.get("unguarded_collection_candidate_count")) != 0
                or _safe_int(summary.get("unsafe_safe_next_action_count")) != 0
                or coverage_hint_bad
            ):
                bad_api_linkage_summary_artifacts.append(str(item.get("path") or item.get("name")))
        if item.get("name") == "qualification_retry_hygiene_json":
            hygiene = (
                item.get("qualification_retry_hygiene")
                if isinstance(item.get("qualification_retry_hygiene"), dict)
                else {}
            )
            if (
                hygiene.get("ok") is not True
                or hygiene.get("mode") != "dry_run"
                or hygiene.get("report_only") is not True
                or hygiene.get("status_update_allowed") is not False
                or hygiene.get("db_writes") is not False
                or hygiene.get("api_calls") is not False
                or hygiene.get("approval_claim") is not False
                or hygiene.get("execution_authorized") is not False
                or hygiene.get("retry_collection_authorized") is not False
                or hygiene.get("automatic_queue_execution_allowed") is not False
                or hygiene.get("authorization_status")
                != "not_authorized_read_only_report"
                or hygiene.get("do_not_call_api") is not True
                or hygiene.get("api_call_allowed_now") is not False
                or _safe_int(hygiene.get("safety_violation_count")) != 0
                or hygiene.get("collection_coverage") is None
                or (_safe_int(hygiene.get("status_count_rows")) or 0) <= 0
                or (
                    hygiene.get("coverage_gap_open") is True
                    and hygiene.get("coverage_gap_normalized_next_safe_action")
                    != "plan_guarded_qualification_collection_for_unattempted_units"
                )
                or (
                    hygiene.get("retry_hygiene_status_scope")
                    != "retry_preflight_only_not_collection_coverage"
                )
            ):
                bad_qualification_retry_hygiene_artifacts.append(str(item.get("path") or item.get("name")))
        if item.get("name") == "qualification_collection_coverage_plan_json":
            plan = (
                item.get("qualification_collection_coverage_plan")
                if isinstance(item.get("qualification_collection_coverage_plan"), dict)
                else {}
            )
            if (
                plan.get("schema") != "ncs_qualification_collection_coverage_plan_v1"
                or plan.get("ok") is not True
                or plan.get("report_only") is not True
                or plan.get("status_update_allowed") is not False
                or plan.get("db_writes") is not False
                or plan.get("api_calls") is not False
                or plan.get("execution_authorized") is not False
                or plan.get("top_level_automatic_queue_execution_allowed") is not False
                or plan.get("automatic_collection_allowed_now") is not False
                or plan.get("operator_timed_guarded_api_commands_only") is not True
                or plan.get("human_review_status_updates") is not False
                or plan.get("approval_claim") is not False
                or not plan.get("checkpoint_path")
                or plan.get("batch_commands_have_ncs006_checkpoint_values") is not True
                or plan.get("batch_commands_checkpoint_values_match_plan") is not True
                or not isinstance(plan.get("target_ratio"), (int, float))
                or _safe_int(plan.get("batch_size")) is None
                or (_safe_int(plan.get("batch_size")) or 0) <= 0
                or _safe_int(plan.get("total_unit_count")) is None
                or (_safe_int(plan.get("total_unit_count")) or 0) <= 0
                or _safe_int(plan.get("attempted_unit_count")) is None
                or plan.get("collection_coverage") is None
                or _safe_int(plan.get("additional_attempted_units_needed")) is None
                or _safe_int(plan.get("estimated_batch_count")) is None
                or _safe_int(plan.get("batch_count")) != _safe_int(plan.get("estimated_batch_count"))
                or _safe_int(plan.get("unsafe_batch_count")) != 0
                or plan.get("raw_batch_count_matches_batches") is not True
                or plan.get("raw_unsafe_batch_count_matches_batches") is not True
                or _safe_int(plan.get("raw_unsafe_batches_count")) != 0
                or plan.get("raw_unsafe_batches_match_batches") is not True
                or plan.get("must_run_qualification_retry_hygiene_first") is not True
                or plan.get("must_use_ncs006_checkpoint_path") is not True
                or plan.get("must_not_write_human_review_statuses") is not True
                or plan.get("operator_timing_required") is not True
                or plan.get("operator_must_confirm_api_timing") is not True
                or plan.get("batch_commands_are_operator_timed") is not True
                or plan.get("batch_commands_are_not_queue_items") is not True
                or plan.get("batch_commands_checkpoint_path_must_match_plan") is not True
                or plan.get("automatic_queue_execution_allowed") is not False
                or plan.get("forbidden_status_updates_exact") is not True
            ):
                bad_qualification_coverage_plan_artifacts.append(str(item.get("path") or item.get("name")))
        if item.get("name") == "human_review_provenance_reconfirmation_packet_json":
            if item.get("content_sha256"):
                provenance_reconfirmation_lineage_hashes["packet"] = str(
                    item.get("content_sha256")
                )
            packet = (
                item.get("human_review_provenance_reconfirmation_packet")
                if isinstance(
                    item.get("human_review_provenance_reconfirmation_packet"),
                    dict,
                )
                else {}
            )
            row_count = _safe_int(packet.get("row_count"))
            legacy_count = _safe_int(packet.get("legacy_status_needs_reconfirmation_count"))
            if (
                packet.get("schema")
                != "aihr_human_review_provenance_reconfirmation_packet_v1"
                or packet.get("ok") is not True
                or packet.get("contract_ok") is not True
                or packet.get("status_update_allowed") is not False
                or packet.get("db_writes") is not False
                or packet.get("approval_claim") is not False
                or packet.get("human_decision_required") is not True
                or row_count is None
                or row_count <= 0
                or legacy_count is None
            ):
                bad_provenance_reconfirmation_artifacts.append(
                    str(item.get("path") or item.get("name"))
                )
        if item.get("name") == "human_review_provenance_reconfirmation_decision_sheet_json":
            sheet = (
                item.get("human_review_provenance_reconfirmation_decision_sheet")
                if isinstance(
                    item.get("human_review_provenance_reconfirmation_decision_sheet"),
                    dict,
                )
                else {}
            )
            row_count = _safe_int(sheet.get("row_count"))
            blank_count = _safe_int(sheet.get("blank_decision_count"))
            completed_count = _safe_int(sheet.get("completed_decision_count"))
            if sheet.get("source_packet_sha256"):
                provenance_reconfirmation_lineage_hashes["decision_sheet"] = str(
                    sheet.get("source_packet_sha256")
                )
            if (
                sheet.get("schema") != "aihr_provenance_reconfirmation_decision_sheet_v1"
                or sheet.get("ok") is not True
                or sheet.get("contract_ok") is not True
                or sheet.get("source_packet_schema")
                != "aihr_human_review_provenance_reconfirmation_packet_v1"
                or not sheet.get("source_packet_sha256")
                or sheet.get("source_packet_contract_ok") is not True
                or sheet.get("source_packet_row_identity_issue_count") != 0
                or sheet.get("row_safety_flag_type_issue_count") != 0
                or sheet.get("status_update_allowed") is not False
                or sheet.get("db_writes") is not False
                or sheet.get("approval_claim") is not False
                or sheet.get("human_decision_required") is not True
                or row_count is None
                or row_count <= 0
                or blank_count is None
                or completed_count is None
                or blank_count + completed_count != row_count
            ):
                bad_provenance_reconfirmation_artifacts.append(
                    str(item.get("path") or item.get("name"))
                )
        if item.get("name") == "human_review_provenance_reconfirmation_decision_audit_json":
            audit = (
                item.get("human_review_provenance_reconfirmation_decision_audit")
                if isinstance(
                    item.get("human_review_provenance_reconfirmation_decision_audit"),
                    dict,
                )
                else {}
            )
            row_count = _safe_int(audit.get("row_count"))
            invalid_decisions = _safe_int(audit.get("invalid_decision_count"))
            invalid_evidence = _safe_int(audit.get("invalid_evidence_refs_json_count"))
            invalid_reviewer = _safe_int(audit.get("invalid_reviewer_id_count"))
            invalid_reviewed_at = _safe_int(audit.get("invalid_reviewed_at_count"))
            source_packet_row_count = _safe_int(audit.get("source_packet_row_count"))
            duplicate_csv_keys = _safe_int(audit.get("duplicate_csv_key_count"))
            missing_packet_rows = _safe_int(audit.get("missing_packet_row_count"))
            unexpected_csv_rows = _safe_int(audit.get("unexpected_csv_row_count"))
            source_packet_missing = _safe_int(
                audit.get("source_decision_packet_not_found_count")
            )
            source_packet_not_portable = _safe_int(
                audit.get("source_decision_packet_not_portable_count")
            )
            source_packet_unsupported_type = _safe_int(
                audit.get("source_decision_packet_unsupported_type_count")
            )
            source_packet_unrecognized = _safe_int(
                audit.get("source_decision_packet_unrecognized_count")
            )
            source_identity_mismatch = _safe_int(
                audit.get("source_identity_mismatch_count")
            )
            unsafe_flags = _safe_int(audit.get("unsafe_flag_count"))
            if audit.get("source_packet_sha256"):
                provenance_reconfirmation_lineage_hashes["decision_audit"] = str(
                    audit.get("source_packet_sha256")
                )
            if (
                audit.get("schema") != "aihr_provenance_reconfirmation_decision_audit_v1"
                or audit.get("ok") is not True
                or audit.get("contract_ok") is not True
                or audit.get("source_packet_schema")
                != "aihr_human_review_provenance_reconfirmation_packet_v1"
                or not audit.get("source_packet_sha256")
                or audit.get("source_packet_contract_ok") is not True
                or audit.get("source_packet_row_identity_issue_count") != 0
                or audit.get("status_update_allowed") is not False
                or audit.get("db_writes") is not False
                or audit.get("approval_claim") is not False
                or audit.get("guarded_apply_ready") is not False
                or row_count is None
                or row_count <= 0
                or source_packet_row_count != row_count
                or duplicate_csv_keys != 0
                or missing_packet_rows != 0
                or unexpected_csv_rows != 0
                or source_packet_missing != 0
                or source_packet_not_portable != 0
                or source_packet_unsupported_type != 0
                or source_packet_unrecognized != 0
                or source_identity_mismatch != 0
                or unsafe_flags != 0
                or invalid_decisions != 0
                or invalid_evidence != 0
                or invalid_reviewer != 0
                or invalid_reviewed_at != 0
            ):
                bad_provenance_reconfirmation_artifacts.append(
                    str(item.get("path") or item.get("name"))
                )
    lineage_hash_values = {
        value for value in provenance_reconfirmation_lineage_hashes.values() if value
    }
    if len(lineage_hash_values) > 1:
        bad_provenance_reconfirmation_artifacts.append(
            "provenance_reconfirmation_lineage_mismatch"
        )
    for label, source_queue_path in (
        ("queue_status_api", live_queue_status_source_queue_path),
        ("agent_queue_run_api", live_queue_run_source_queue_path),
    ):
        if not release_readiness_queue_path:
            bad_live_queue_source_artifacts.append(
                f"{label}:expected_release_queue_missing"
            )
        elif not source_queue_path:
            bad_live_queue_source_artifacts.append(
                f"{label}:source_queue_path_missing"
            )
        elif not _queue_source_path_matches(
            source_queue_path,
            release_readiness_queue_path,
        ):
            bad_live_queue_source_artifacts.append(f"{label}:source={source_queue_path}")
    checks.append(
        {
            "name": "Static artifact snapshot",
            "ok": (
                bool(static_artifacts)
                and not bad_static_artifacts
                and not missing_static_artifact_names
                and not bad_ontology_education_audits
                and not bad_review_workflow_handoffs
                and not bad_human_review_backlogs
                and not bad_goal_completion_audits
                and not bad_release_readiness_artifacts
                and not bad_checkpoint_artifacts
                and not bad_queue_status_artifacts
                and not bad_queue_status_raw_auto_start_artifacts
                and not bad_queue_run_artifacts
                and not bad_queue_source_artifacts
                and not bad_queue_source_contract_artifacts
                and not bad_role_static_artifacts
                and not bad_query_route_contract_audit_artifacts
                and not bad_api_linkage_summary_artifacts
                and not bad_qualification_retry_hygiene_artifacts
                and not bad_qualification_coverage_plan_artifacts
                and not bad_provenance_reconfirmation_artifacts
                and not bad_local_path_artifacts
            ),
            "detail": (
                f"count={len(static_artifacts)}"
                if (
                    not bad_static_artifacts
                    and not missing_static_artifact_names
                    and not bad_ontology_education_audits
                    and not bad_review_workflow_handoffs
                    and not bad_human_review_backlogs
                    and not bad_goal_completion_audits
                    and not bad_release_readiness_artifacts
                    and not bad_checkpoint_artifacts
                    and not bad_queue_status_artifacts
                    and not bad_queue_status_raw_auto_start_artifacts
                    and not bad_queue_run_artifacts
                    and not bad_queue_source_artifacts
                    and not bad_queue_source_contract_artifacts
                    and not bad_role_static_artifacts
                    and not bad_query_route_contract_audit_artifacts
                    and not bad_api_linkage_summary_artifacts
                    and not bad_qualification_retry_hygiene_artifacts
                    and not bad_qualification_coverage_plan_artifacts
                    and not bad_provenance_reconfirmation_artifacts
                    and not bad_local_path_artifacts
                )
                else "bad="
                + ",".join(bad_static_artifacts[:10])
                + "; missing="
                + ",".join(missing_static_artifact_names[:10])
                + "; bad_ontology_education_audit="
                + ",".join(bad_ontology_education_audits[:10])
                + "; bad_review_workflow_handoff="
                + ",".join(bad_review_workflow_handoffs[:10])
                + "; bad_human_review_backlog="
                + ",".join(bad_human_review_backlogs[:10])
                + "; bad_goal_completion_audit="
                + ",".join(bad_goal_completion_audits[:10])
                + "; bad_release_readiness="
                + ",".join(bad_release_readiness_artifacts[:10])
                + "; bad_checkpoint="
                + ",".join(bad_checkpoint_artifacts[:10])
                + "; review_gated_checkpoint="
                + ",".join(review_gated_checkpoint_artifacts[:10])
                + "; bad_queue_status="
                + ",".join(bad_queue_status_artifacts[:10])
                + "; bad_queue_status_raw_auto_start="
                + ",".join(bad_queue_status_raw_auto_start_artifacts[:10])
                + "; bad_queue_run="
                + ",".join(bad_queue_run_artifacts[:10])
                + "; bad_queue_source="
                + ",".join(bad_queue_source_artifacts[:10])
                + "; bad_queue_source_contract="
                + ",".join(bad_queue_source_contract_artifacts[:10])
                + "; bad_role="
                + ",".join(bad_role_static_artifacts[:10])
                + "; bad_query_route_contract_audit="
                + ",".join(bad_query_route_contract_audit_artifacts[:10])
                + "; bad_api_linkage_summary="
                + ",".join(bad_api_linkage_summary_artifacts[:10])
                + "; bad_qualification_retry_hygiene="
                + ",".join(bad_qualification_retry_hygiene_artifacts[:10])
                + "; bad_qualification_coverage_plan="
                + ",".join(bad_qualification_coverage_plan_artifacts[:10])
                + "; bad_provenance_reconfirmation="
                + ",".join(bad_provenance_reconfirmation_artifacts[:10])
                + "; bad_local_path="
                + ",".join(bad_local_path_artifacts[:10])
                + "; core_date_mismatch="
                + ("true" if core_static_artifact_date_mismatch else "false")
                + "; all_dates_mixed="
                + ("true" if mixed_static_artifact_dates else "false")
            ),
            "review_gated_checkpoint_artifacts": review_gated_checkpoint_artifacts,
            "review_gated_checkpoint_details": review_gated_checkpoint_details,
            "bad_role_static_artifacts": bad_role_static_artifacts,
            "bad_queue_source_contract_artifacts": bad_queue_source_contract_artifacts,
            "bad_query_route_contract_audit_artifacts": bad_query_route_contract_audit_artifacts,
            "provenance_reconfirmation_lineage_hashes": provenance_reconfirmation_lineage_hashes,
        }
    )
    checks.append(
        {
            "name": "Static artifact date consistency",
            "ok": not core_static_artifact_date_mismatch
            and not core_static_artifact_stamp_family_mismatch,
            "detail": (
                f"dashboard_date={dashboard_artifact_date}, "
                f"dashboard_stamp_family={dashboard_artifact_stamp_family}, "
                f"core_static_dates={core_static_artifact_dates}, "
                f"core_static_stamp_families={core_static_artifact_stamp_families}, "
                f"all_static_dates={static_artifact_dates}, "
                f"all_static_stamp_families={static_artifact_stamp_families}"
            ),
        }
    )
    checks.append(
        {
            "name": "Static artifact freshness",
            "ok": not stale_static_artifacts,
            "detail": (
                "content_sha256_matches_current_files"
                if not stale_static_artifacts
                else ", ".join(stale_static_artifacts[:10])
            ),
        }
    )
    checks.append(
        {
            "name": "Queue status artifact",
            "ok": "queue_status_json" in static_artifact_names and not bad_queue_status_artifacts,
            "detail": (
                "present_and_guarded"
                if "queue_status_json" in static_artifact_names and not bad_queue_status_artifacts
                else "missing"
                if "queue_status_json" not in static_artifact_names
                else ", ".join(bad_queue_status_artifacts[:10])
            ),
        }
    )
    checks.append(
        {
            "name": "Queue status raw auto-start artifact",
            "ok": (
                "queue_status_json" in static_artifact_names
                and not bad_queue_status_raw_auto_start_artifacts
            ),
            "detail": (
                "present_and_safe"
                if (
                    "queue_status_json" in static_artifact_names
                    and not bad_queue_status_raw_auto_start_artifacts
                )
                else "missing"
                if "queue_status_json" not in static_artifact_names
                else ", ".join(bad_queue_status_raw_auto_start_artifacts[:10])
            ),
        }
    )
    checks.append(
        {
            "name": "Queue raw auto-start contract",
            "ok": not raw_queue_status_auto_start_violations,
            "detail": (
                "none"
                if not raw_queue_status_auto_start_violations
                else ", ".join(raw_queue_status_auto_start_violations[:10])
            ),
        }
    )
    checks.append(
        {
            "name": "Queue run artifact",
            "ok": "queue_run_json" in static_artifact_names and not bad_queue_run_artifacts,
            "detail": (
                "present_actual_and_bounded"
                if "queue_run_json" in static_artifact_names and not bad_queue_run_artifacts
                else "missing"
                if "queue_run_json" not in static_artifact_names
                else ", ".join(bad_queue_run_artifacts[:10])
            ),
        }
    )
    checks.append(
        {
            "name": "Queue source path consistency",
            "ok": not bad_queue_source_artifacts,
            "detail": (
                "source_queue_path_matches_release_queue"
                if not bad_queue_source_artifacts
                else (
                    f"expected={release_readiness_queue_path}; "
                    f"bad={','.join(bad_queue_source_artifacts[:10])}"
                )
            ),
        }
    )
    checks.append(
        {
            "name": "Live queue source path consistency",
            "ok": not bad_live_queue_source_artifacts,
            "detail": (
                "source_queue_path_matches_release_queue"
                if not bad_live_queue_source_artifacts
                else (
                    f"expected={release_readiness_queue_path}; "
                    f"bad={','.join(bad_live_queue_source_artifacts[:10])}"
                )
            ),
        }
    )
    checks.append(
        {
            "name": "Queue source artifact contract",
            "ok": not bad_queue_source_contract_artifacts,
            "detail": (
                "source_queue_contract_ok"
                if not bad_queue_source_contract_artifacts
                else ", ".join(bad_queue_source_contract_artifacts[:10])
            ),
        }
    )
    checks.append(
        {
            "name": "Guide surface audit artifact",
            "ok": "guide_surface_audit_json" in static_artifact_names and not bad_guide_surface_audits,
            "detail": (
                "present_and_safe"
                if "guide_surface_audit_json" in static_artifact_names and not bad_guide_surface_audits
                else "missing"
                if "guide_surface_audit_json" not in static_artifact_names
                else ", ".join(bad_guide_surface_audits[:10])
            ),
        }
    )
    checks.append(
        {
            "name": "Ontology transferability education audit artifact",
            "ok": (
                "ontology_transferability_education_audit_json" in static_artifact_names
                and not bad_ontology_education_audits
            ),
            "detail": (
                "present_review_gated"
                if (
                    "ontology_transferability_education_audit_json" in static_artifact_names
                    and not bad_ontology_education_audits
                )
                else "missing"
                if "ontology_transferability_education_audit_json" not in static_artifact_names
                else ", ".join(bad_ontology_education_audits[:10])
            ),
        }
    )
    checks.append(
        {
            "name": "Review workflow handoff artifact",
            "ok": (
                "review_workflow_handoff_json" in static_artifact_names
                and not bad_review_workflow_handoffs
            ),
            "detail": (
                "present_and_safe"
                if (
                    "review_workflow_handoff_json" in static_artifact_names
                    and not bad_review_workflow_handoffs
                )
                else "missing"
                if "review_workflow_handoff_json" not in static_artifact_names
                else ", ".join(bad_review_workflow_handoffs[:10])
            ),
        }
    )
    checks.append(
        {
            "name": "Human review backlog artifact",
            "ok": (
                "human_review_backlog_json" in static_artifact_names
                and not bad_human_review_backlogs
            ),
            "detail": (
                "present_and_safe"
                if (
                    "human_review_backlog_json" in static_artifact_names
                    and not bad_human_review_backlogs
                )
                else "missing"
                if "human_review_backlog_json" not in static_artifact_names
                else ", ".join(bad_human_review_backlogs[:10])
            ),
        }
    )
    checks.append(
        {
            "name": "Goal completion audit artifact",
            "ok": (
                "goal_completion_audit_json" in static_artifact_names
                and not bad_goal_completion_audits
            ),
            "detail": (
                "present_and_safe"
                if (
                    "goal_completion_audit_json" in static_artifact_names
                    and not bad_goal_completion_audits
                )
                else "missing"
                if "goal_completion_audit_json" not in static_artifact_names
                else ", ".join(bad_goal_completion_audits[:10])
            ),
        }
    )
    for check in checks:
        if not check["ok"]:
            failures.append({"check": check["name"], "detail": check["detail"]})

    artifact.update(
        {
            "ok": not failures,
            "schema": payload.get("schema"),
            "scenario_count": payload.get("scenario_count"),
            "queue_status_summary": queue_summary,
            "review_chain_safety_summary": review_chain_summary,
            "static_artifact_dates": static_artifact_dates,
            "static_artifact_stamp_families": static_artifact_stamp_families,
            "core_static_artifact_dates": core_static_artifact_dates,
            "core_static_artifact_stamp_families": core_static_artifact_stamp_families,
            "mixed_static_artifact_dates": mixed_static_artifact_dates,
            "core_static_artifact_date_mismatch": core_static_artifact_date_mismatch,
            "core_static_artifact_stamp_family_mismatch": (
                core_static_artifact_stamp_family_mismatch
            ),
            "stale_static_artifacts": stale_static_artifacts,
            "deferred_readiness_snapshot_issues": deferred_readiness_snapshot_issues,
            "pending_release_readiness_path": (
                str(pending_release_readiness_path)
                if pending_release_readiness_path is not None
                else None
            ),
            "freshness_hash_skip_names": freshness_hash_skip_names,
            "freshness_hash_skip_reason": {
                name: freshness_hash_skip_reason[name]
                for name in freshness_hash_skip_names
                if name in freshness_hash_skip_reason
            },
            "live_plan_summaries": live_summaries,
            "static_artifacts": static_artifacts,
            "checks": checks,
        }
    )
    return {
        "ok": not failures,
        "artifact": artifact,
        "failure_count": len(failures),
        "failures": failures,
    }


def _readability_path_keys(value: Any) -> set[str]:
    if value is None:
        return set()
    text = str(value).strip()
    if not text:
        return set()
    candidates = {text}
    path = Path(text)
    candidates.add(str(path))
    try:
        candidates.add(str(path.resolve()))
    except OSError:
        pass
    for candidate in list(candidates):
        try:
            candidate_path = Path(candidate)
            if candidate_path.is_absolute():
                candidates.add(str(candidate_path.relative_to(ROOT)))
        except ValueError:
            pass
    return {
        candidate.replace("/", "\\").lower().lstrip(".\\")
        for candidate in candidates
        if candidate
    }


def _finding_matches_active_artifact(finding: dict[str, Any], active_path_keys: set[str]) -> bool:
    finding_keys = _readability_path_keys(finding.get("path"))
    return bool(finding_keys and active_path_keys and finding_keys.intersection(active_path_keys))


def build_review_artifact_readability_contract(
    path: Path | None,
    *,
    active_artifact_paths: list[Any] | None = None,
) -> dict[str, Any] | None:
    if path is None:
        return None
    artifact: dict[str, Any] = {"path": str(path)}
    failures: list[dict[str, Any]] = []
    if not path.exists():
        failures.append({"check": "artifact_exists", "detail": "missing"})
        return {
            "ok": False,
            "artifact": artifact | {"ok": False, "error": "missing_readability_audit"},
            "failure_count": len(failures),
            "failures": failures,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        failures.append({"check": "artifact_json", "detail": str(exc)})
        return {
            "ok": False,
            "artifact": artifact | {"ok": False, "error": "invalid_json", "detail": str(exc)},
            "failure_count": len(failures),
            "failures": failures,
        }
    if not isinstance(payload, dict):
        failures.append({"check": "artifact_shape", "detail": "not_json_object"})
        return {
            "ok": False,
            "artifact": artifact | {"ok": False, "error": "not_json_object"},
            "failure_count": len(failures),
            "failures": failures,
        }

    findings = payload.get("findings") if isinstance(payload.get("findings"), list) else []
    finding_count = _safe_int(payload.get("finding_count"))
    active_path_keys: set[str] = set()
    for active_path in active_artifact_paths or []:
        active_path_keys.update(_readability_path_keys(active_path))
    scoped_findings = [
        finding
        for finding in findings
        if isinstance(finding, dict) and _finding_matches_active_artifact(finding, active_path_keys)
    ]
    blocking_findings = scoped_findings
    artifact.update(
        {
            "ok": True,
            "audit_ok": payload.get("ok") is True,
            "schema": payload.get("schema"),
            "status": payload.get("status"),
            "artifact_count": _safe_int(payload.get("artifact_count")),
            "finding_count": finding_count,
            "scoped_artifact_count": len(active_artifact_paths or []),
            "scoped_finding_count": len(scoped_findings),
            "blocking_finding_count": len(blocking_findings),
            "status_update_allowed": payload.get("status_update_allowed"),
            "db_writes": payload.get("db_writes"),
            "approval_claim": payload.get("approval_claim"),
            "human_decision_required": payload.get("human_decision_required"),
        }
    )
    if payload.get("schema") != REVIEW_ARTIFACT_READABILITY_AUDIT_SCHEMA:
        failures.append(
            {
                "check": "schema",
                "detail": payload.get("schema"),
                "threshold": REVIEW_ARTIFACT_READABILITY_AUDIT_SCHEMA,
            }
        )
    safety_flags = {
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_decision_required": True,
    }
    for field, expected in safety_flags.items():
        if payload.get(field) is not expected:
            failures.append(
                {
                    "check": field,
                    "detail": payload.get(field),
                    "threshold": expected,
                }
            )
    if finding_count is None:
        failures.append({"check": "finding_count", "detail": payload.get("finding_count")})
    if blocking_findings:
        failures.append(
            {
                "check": "active_artifact_readability",
                "detail": "readability findings overlap active release proof/operator artifacts",
                "value": len(blocking_findings),
                "threshold": "0 active artifact findings",
                "findings": blocking_findings[:20],
            }
        )
    artifact["ok"] = not failures
    return {
        "ok": not failures,
        "artifact": artifact,
        "failure_count": len(failures),
        "failures": failures,
    }


def build_release_readiness(
    quality_report: dict[str, Any],
    contract: dict[str, Any],
    *,
    demo_contract: dict[str, Any] | None = None,
    dashboard_surface_contract: dict[str, Any] | None = None,
    review_readability_contract: dict[str, Any] | None = None,
    dashboard_static_artifact_dir: str | Path | None = None,
    quality_report_path: str | Path | None = None,
    quality_report_markdown_path: str | Path | None = None,
    release_readiness_markdown_path: str | Path | None = None,
    review_priority_report_path: str | Path | None = None,
    review_priority_markdown_path: str | Path | None = None,
    min_trusted_scenarios: int = 10,
    min_qualification_coverage: float = 0.9,
    artifact_date: str | None = None,
    artifact_date_contract: dict[str, Any] | None = None,
    artifact_lineage_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gates = _gate_by_name(quality_report)
    summary = quality_report.get("summary") or {}
    contract_surface = contract.get("surface") or {}
    mcp_contract_checks = build_mcp_contract_checks(contract)
    productization_strategy_check = build_productization_strategy_check()
    deployment_runbook_check = build_deployment_runbook_check()
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    fail_count = int(summary.get("fail_count") or 0)
    if fail_count:
        blockers.append(
            {
                "category": "engineering_hygiene",
                "name": "quality_gate_failures",
                "message": "Quality gates have fail-severity results.",
                "value": fail_count,
            }
        )

    for check in mcp_contract_checks:
        if not check["ok"]:
            blockers.append(
                {
                    "category": "mcp_contract",
                    "name": check["name"],
                    "message": "Public MCP contract does not satisfy the Law MCP-grade surface/router contract.",
                    "value": check["detail"],
                    "threshold": "pass",
                }
            )

    if not productization_strategy_check.get("ok"):
        blockers.append(
            {
                "category": "engineering_hygiene",
                "name": "productization_strategy",
                "message": "Productization and deployment strategy document is missing or incomplete.",
                "value": productization_strategy_check.get("path"),
                "threshold": "required strategy markers present",
                "details": productization_strategy_check,
            }
        )

    if not deployment_runbook_check.get("ok"):
        blockers.append(
            {
                "category": "engineering_hygiene",
                "name": "deployment_runbook",
                "message": "Deployment runbook is missing or incomplete.",
                "value": deployment_runbook_check.get("path"),
                "threshold": "required deployment markers present",
                "details": deployment_runbook_check,
            }
        )

    artifact_date_contract = artifact_date_contract or {}
    artifact_date_contract_ok = True
    for section_name, section in artifact_date_contract.items():
        if not isinstance(section, dict) or section.get("ok") is not False:
            continue
        artifact_date_contract_ok = False
        blockers.append(
            {
                "category": "artifact_contract",
                "name": f"artifact_date:{section_name}",
                "message": "Release-readiness core artifact paths use conflicting date stamps.",
                "value": section.get("dates"),
                "threshold": "one date family",
                "details": section,
            }
        )
    artifact_lineage_contract_ok = (
        artifact_lineage_contract is None or artifact_lineage_contract.get("ok") is not False
    )
    if artifact_lineage_contract is not None and artifact_lineage_contract.get("ok") is False:
        blockers.append(
            {
                "category": "artifact_contract",
                "name": "artifact_lineage:dashboard_verification",
                "message": "Dashboard verification proof does not point to the release-readiness and agent queue artifacts being emitted.",
                "value": {
                    "release_path_ok": artifact_lineage_contract.get("release_path_ok"),
                    "queue_path_ok": artifact_lineage_contract.get("queue_path_ok"),
                    "dashboard_verification_content_hash_ok": (
                        artifact_lineage_contract.get(
                            "dashboard_verification_content_hash_ok"
                        )
                    ),
                },
                "threshold": "dashboard proof lineage matches release outputs",
                "details": artifact_lineage_contract,
            }
        )

    operator_tool_count = int(contract_surface.get("operator_tool_count") or 0)
    if operator_tool_count and "Operator tools hidden" not in {blocker["name"] for blocker in blockers}:
        blockers.append(
            {
                "category": "mcp_contract",
                "name": "operator_tools_exposed_publicly",
                "message": "Operator tools should be hidden in the default public contract.",
                "value": operator_tool_count,
            }
        )

    for gate_name in sorted(HUMAN_REVIEW_GATES):
        gate = gates.get(gate_name)
        if not gate:
            blockers.append(_missing_gate_blocker(gate_name))
        elif gate.get("status") != "pass":
            blockers.append(
                {
                    "category": "human_review",
                    "name": gate_name,
                    "message": gate.get("message"),
                    "value": gate.get("value"),
                    "threshold": gate.get("threshold"),
                }
            )

    qualification_gate = gates.get("qualification:collection_coverage")
    if not qualification_gate:
        blockers.append(_missing_gate_blocker("qualification:collection_coverage"))
    else:
        coverage = float(qualification_gate.get("value") or 0)
        if coverage < min_qualification_coverage:
            blockers.append(
                {
                    "category": "data_collection",
                    "name": "qualification:collection_coverage",
                    "message": "Qualification collection coverage is below the release target.",
                    "value": coverage,
                    "threshold": f">= {min_qualification_coverage}",
                    "details": qualification_gate.get("details") or {},
                }
            )

    trusted_gate = gates.get("transition_eval:trusted_scenarios")
    if not trusted_gate:
        blockers.append(_missing_gate_blocker("transition_eval:trusted_scenarios"))
    else:
        trusted_count = int(trusted_gate.get("value") or 0)
        if trusted_count < min_trusted_scenarios:
            blockers.append(
                {
                    "category": "evaluation",
                    "name": "transition_eval:trusted_scenarios",
                    "message": "Trusted transition scenarios are too sparse for release-grade evaluation.",
                    "value": trusted_count,
                    "threshold": f">= {min_trusted_scenarios}",
                }
            )

    missing_required_gates = [
        name
        for name in REQUIRED_RELEASE_GATES
        if name not in gates
    ]

    if demo_contract is None:
        blockers.append(
            {
                "category": "demo_contract",
                "name": "aihr_demo_contract",
                "message": "AI-HR demo artifacts were not supplied to release readiness.",
                "value": "missing",
                "threshold": "demo JSON/HTML contract required",
                "details": {"failures": [{"check": "demo_contract_present", "detail": "missing"}]},
            }
        )
    elif not demo_contract.get("ok"):
        blockers.append(
            {
                "category": "demo_contract",
                "name": "aihr_demo_contract",
                "message": "AI-HR demo artifacts do not satisfy the visible prototype contract.",
                "value": demo_contract.get("failure_count"),
                "threshold": "0 failures",
                "details": {"failures": (demo_contract.get("failures") or [])[:20]},
            }
        )

    if dashboard_surface_contract is None:
        blockers.append(
            {
                "category": "dashboard_surface",
                "name": "aihr_dashboard_surface",
                "message": "Running AI-HR dashboard surface verification was not supplied to release readiness.",
                "value": "missing",
                "threshold": "dashboard verification contract required",
                "details": {"failures": [{"check": "dashboard_surface_contract_present", "detail": "missing"}]},
            }
        )
    elif not dashboard_surface_contract.get("ok"):
        blockers.append(
            {
                "category": "dashboard_surface",
                "name": "aihr_dashboard_surface",
                "message": "Running AI-HR dashboard surface verification did not pass.",
                "value": dashboard_surface_contract.get("failure_count"),
                "threshold": "0 failures",
                "details": {"failures": (dashboard_surface_contract.get("failures") or [])[:20]},
            }
        )

    if review_readability_contract is not None and not review_readability_contract.get("ok"):
        readability_artifact = review_readability_contract.get("artifact") or {}
        blockers.append(
            {
                "category": "review_artifact_quality",
                "name": REVIEW_ARTIFACT_READABILITY_BLOCKER,
                "message": "Review artifact readability audit found encoding/display findings or unsafe contract flags.",
                "value": readability_artifact.get("blocking_finding_count"),
                "threshold": "0 active artifact findings and safe report-only flags",
                "details": {
                    "artifact": readability_artifact,
                    "failures": (review_readability_contract.get("failures") or [])[:20],
                    "approval_claim": False,
                    "status_update_allowed": False,
                    "db_writes": False,
                },
            }
        )

    existing_blocker_names = {str(blocker.get("name")) for blocker in blockers}
    for blocker in _human_review_provenance_blockers_from_dashboard_contract(
        dashboard_surface_contract
    ):
        blocker_name = str(blocker.get("name"))
        if blocker_name not in existing_blocker_names:
            blockers.append(blocker)
            existing_blocker_names.add(blocker_name)
            continue
        if blocker_name == HUMAN_REVIEW_PROVENANCE_BLOCKER:
            existing = next(
                item
                for item in blockers
                if str(item.get("name")) == HUMAN_REVIEW_PROVENANCE_BLOCKER
            )
            existing_details = (
                existing.setdefault("details", {})
                if isinstance(existing.get("details"), dict)
                else {}
            )
            incoming_details = (
                blocker.get("details") if isinstance(blocker.get("details"), dict) else {}
            )
            for key in (
                "bad_artifacts",
                "missing_artifacts",
                "lineage_hashes",
                "lineage_mismatch",
            ):
                if key in incoming_details:
                    existing_details[key] = incoming_details[key]
            if incoming_details:
                existing_details["proofset_artifact_issue_count"] = blocker.get("value")
            existing["details"] = existing_details
            existing["message"] = (
                "Trusted human-review rows require packet-backed provenance "
                "reconfirmation, and the proofset artifacts must stay "
                "contract-valid and source-packet hash aligned before they can "
                "support release claims."
            )
            existing["threshold"] = "0 unresolved provenance gaps and 0 proofset artifact issues"

    blocker_names = {str(blocker.get("name")) for blocker in blockers}
    for gate in quality_report.get("gates", []):
        gate_name = str(gate.get("name"))
        if (
            gate.get("status") == "warn"
            and gate_name not in HUMAN_REVIEW_GATES
            and gate_name not in blocker_names
        ):
            warnings.append(
                {
                    "name": gate_name,
                    "message": gate.get("message"),
                    "value": gate.get("value"),
                    "threshold": gate.get("threshold"),
                }
            )

    engineering_hygiene_ok = (
        fail_count == 0
        and not missing_required_gates
        and all(check["ok"] for check in mcp_contract_checks)
        and bool(productization_strategy_check.get("ok"))
        and bool(deployment_runbook_check.get("ok"))
        and bool(demo_contract and demo_contract.get("ok"))
        and bool(dashboard_surface_contract and dashboard_surface_contract.get("ok"))
        and bool(review_readability_contract is None or review_readability_contract.get("ok"))
        and artifact_date_contract_ok
        and artifact_lineage_contract_ok
    )
    for blocker in blockers:
        add_blocker_display_fields(blocker)
    release_ready = not blockers
    guarded_preflight = _guarded_preflight_from_dashboard_contract(dashboard_surface_contract)
    ncs006_checkpoint_path = _dashboard_static_artifact_path(
        dashboard_surface_contract,
        "ncs006_element_api_checkpoint_json",
    )
    next_actions = build_release_next_actions(
        blockers,
        artifact_date=artifact_date,
        dashboard_static_artifact_dir=dashboard_static_artifact_dir,
        quality_report_path=quality_report_path,
        quality_report_markdown_path=quality_report_markdown_path,
        release_readiness_markdown_path=release_readiness_markdown_path,
        review_priority_report_path=review_priority_report_path,
        review_priority_markdown_path=review_priority_markdown_path,
        guarded_preflight=guarded_preflight,
        ncs006_checkpoint_path=ncs006_checkpoint_path,
    )
    release_decision_status = (
        "release_candidate_ready" if release_ready else "blocked_until_requirements_met"
    )
    report = {
        "schema": RELEASE_READINESS_SCHEMA,
        "ok": True,
        "ok_meaning": "report_generated_and_contract_checks_evaluated",
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "release_ready": release_ready,
        "release_decision": {
            "status": release_decision_status,
            "release_ready": release_ready,
            "approval_claim": False,
            "human_decision_required_for_release_claim": True,
            "blocked_by": [blocker.get("name") for blocker in blockers],
            "blocked_by_display_labels": blocker_display_labels(
                [blocker.get("name") for blocker in blockers]
            ),
        },
        "approval_claim": False,
        "engineering_hygiene_ok": engineering_hygiene_ok,
        "blocker_count": len(blockers),
        "warning_count": len(warnings),
        "blockers": blockers,
        "warnings": warnings,
        "next_actions": next_actions,
        "inputs": {
            "quality_status": quality_report.get("status"),
            "quality_summary": summary,
            "contract_surface": contract_surface,
            "min_trusted_scenarios": min_trusted_scenarios,
            "min_qualification_coverage": min_qualification_coverage,
        },
        "artifact_date_contract": artifact_date_contract,
        "artifact_lineage_contract": artifact_lineage_contract,
        "checks": {
            "mcp_contract": mcp_contract_checks,
            "productization_strategy": productization_strategy_check,
            "deployment_runbook": deployment_runbook_check,
            "review_artifact_readability": review_readability_contract,
        },
        "demo_contract": demo_contract,
        "dashboard_surface_contract": dashboard_surface_contract,
        "review_readability_contract": review_readability_contract,
    }
    report["agent_work_queue"] = build_agent_work_queue(report, artifact_date=artifact_date)
    return report


def write_markdown(report: dict[str, Any], out_path: Path) -> None:
    report_artifact_stamp = _artifact_stamp_from_text(
        str(report.get("markdown_path") or out_path or "")
    )
    lines = [
        "# NCS MCP Release Readiness",
        "",
        f"- schema: {report.get('schema')}",
        f"- ok: {str(report.get('ok')).lower()} ({report.get('ok_meaning')})",
        f"- report_only: {str(report.get('report_only')).lower()}",
        f"- status_update_allowed: {str(report.get('status_update_allowed')).lower()}",
        f"- db_writes: {str(report.get('db_writes')).lower()}",
        f"- release_ready: {str(report.get('release_ready')).lower()}",
        f"- release_decision_status: {(report.get('release_decision') or {}).get('status')}",
        f"- approval_claim: {str(report.get('approval_claim')).lower()}",
        f"- engineering_hygiene_ok: {str(report.get('engineering_hygiene_ok')).lower()}",
        f"- blocker_count: {report.get('blocker_count')}",
        f"- warning_count: {report.get('warning_count')}",
        "",
        "## Blockers",
        "",
    ]
    blockers = report.get("blockers") or []
    if not blockers:
        lines.append("- none")
    for blocker in blockers:
        name = str(blocker.get("name") or "")
        label = str(blocker.get("display_label") or blocker_display_label(name))
        message = blocker.get("display_message") or blocker_display_message(
            name, blocker.get("message")
        )
        lines.append(
            "- "
            + f"[{blocker.get('category')}] {label} (machine: `{name}`): "
            + f"{message} "
            + f"(value={blocker.get('value')}, threshold={blocker.get('threshold')})"
        )
    lines.extend(["", "## Warnings", ""])
    warnings = report.get("warnings") or []
    if not warnings:
        lines.append("- none")
    for warning in warnings:
        lines.append(
            "- "
            + f"{warning.get('name')}: {warning.get('message')} "
            + f"(value={warning.get('value')}, threshold={warning.get('threshold')})"
        )
    artifact_date_contract = report.get("artifact_date_contract") or {}
    if artifact_date_contract:
        lines.extend(["", "## Artifact Date Contract", ""])
        for section_name, section in artifact_date_contract.items():
            if not isinstance(section, dict):
                continue
            lines.append(
                "- "
                + f"{section_name}: ok={str(section.get('ok')).lower()}, "
                + f"expected_date={section.get('expected_date')}, "
                + f"expected_stamp={section.get('expected_stamp')}, "
                + f"expected_stamp_family={section.get('expected_stamp_family')}, "
                + f"dates={section.get('dates')}, "
                + f"stamps={section.get('stamps')}, "
                + f"stamp_families={section.get('stamp_families')}, "
                + f"mismatched_roles={section.get('mismatched_roles')}"
                + f", mismatched_stamp_roles={section.get('mismatched_stamp_roles')}"
            )
    artifact_lineage_contract = report.get("artifact_lineage_contract")
    if isinstance(artifact_lineage_contract, dict):
        lines.extend(["", "## Artifact Lineage Contract", ""])
        lines.append(
            "- dashboard_verification: "
            + f"ok={str(artifact_lineage_contract.get('ok')).lower()}, "
            + f"checked={str(artifact_lineage_contract.get('checked')).lower()}, "
            + f"release_path_ok={artifact_lineage_contract.get('release_path_ok')}, "
            + f"queue_path_ok={artifact_lineage_contract.get('queue_path_ok')}"
        )
    next_actions = report.get("next_actions") or []
    lines.extend(["", "## Next Actions", ""])
    if not next_actions:
        lines.append("- none")
    for item in next_actions:
        preflight = item.get("preflight") if isinstance(item.get("preflight"), dict) else None
        preflight_text = ""
        if preflight:
            violations = ", ".join(preflight.get("safety_violations") or []) or "none"
            preflight_text = (
                f" Preflight: state={preflight.get('state')}, "
                f"preflight_ok={str(preflight.get('preflight_ok')).lower()}, "
                f"api_call_allowed_now={str(preflight.get('api_call_allowed_now')).lower()}, "
                "qualification_retry_allowed_now="
                f"{str(preflight.get('qualification_retry_allowed_now')).lower()}, "
                f"next_safe_action_status={preflight.get('next_safe_action_status')}, "
                f"safety={violations}. "
            )
        prerequisite_commands = _prerequisite_commands_for_command(
            str(item.get("command") or ""),
            artifact_date=report_artifact_stamp
            or _artifact_stamp_from_text(str(item.get("command") or "")),
        )
        prerequisite_text = ""
        if prerequisite_commands:
            prerequisite_text = (
                " Prerequisite: "
                + "; ".join(f"`{command}`" for command in prerequisite_commands)
                + ". "
            )
        blocker_name = str(item.get("blocker") or "")
        blocker_label = str(item.get("blocker_display_label") or blocker_display_label(blocker_name))
        lines.append(
            "- "
            + f"[{item.get('owner')}] {blocker_label} (blocker `{blocker_name}`): {item.get('action')} "
            + preflight_text
            + prerequisite_text
            + f"`{_markdown_guarded_command(item)}`"
        )
    queue = report.get("agent_work_queue") or {}
    queue_items = queue.get("items") or []
    lines.extend(["", "## Agent Work Queue", ""])
    lines.append(
        "- final automatic execution is allowed only when agent-queue-status reports "
        "`can_start_automated=true` and `mutation_policy=regenerate_reports_only`."
    )
    if not queue_items:
        lines.append("- none")
    for item in queue_items:
        preflight = item.get("preflight") if isinstance(item.get("preflight"), dict) else None
        preflight_text = ""
        if preflight:
            preflight_text = (
                f", preflight_state={preflight.get('state')}, "
                f"queue_status_can_start_automated={str(preflight.get('can_start_automated')).lower()}, "
                f"api_call_allowed_now={str(preflight.get('api_call_allowed_now')).lower()}, "
                "qualification_retry_allowed_now="
                f"{str(preflight.get('qualification_retry_allowed_now')).lower()}"
            )
        blocker_name = str(item.get("blocker") or "")
        blocker_label = str(item.get("blocker_display_label") or blocker_display_label(blocker_name))
        lines.append(
            "- "
            + f"[p{item.get('priority')}] {item.get('owner')} -> {item.get('agent_file')}: "
            + f"{blocker_label} (blocker `{blocker_name}`) "
            + f"(auto_runnable={str(item.get('auto_runnable')).lower()}, "
            + f"policy={item.get('mutation_policy')}{preflight_text})"
        )
    mcp_checks = ((report.get("checks") or {}).get("mcp_contract") or [])
    lines.extend(["", "## MCP Contract Checks", ""])
    if not mcp_checks:
        lines.append("- not checked")
    for check in mcp_checks:
        status = "pass" if check.get("ok") else "fail"
        lines.append(f"- {status}: {check.get('name')} ({check.get('detail')})")
    productization_check = (report.get("checks") or {}).get("productization_strategy") or {}
    lines.extend(["", "## Productization Strategy", ""])
    if not productization_check:
        lines.append("- not checked")
    else:
        status = "pass" if productization_check.get("ok") else "fail"
        missing = productization_check.get("missing_markers") or []
        lines.append(
            "- "
            + f"{status}: {productization_check.get('path')} "
            + f"({productization_check.get('detail')})"
        )
        if missing:
            lines.append("- missing_markers: " + ", ".join(str(item) for item in missing))
    deployment_runbook_check = (report.get("checks") or {}).get("deployment_runbook") or {}
    lines.extend(["", "## Deployment Runbook", ""])
    if not deployment_runbook_check:
        lines.append("- not checked")
    else:
        status = "pass" if deployment_runbook_check.get("ok") else "fail"
        missing = deployment_runbook_check.get("missing_markers") or []
        lines.append(
            "- "
            + f"{status}: {deployment_runbook_check.get('path')} "
            + f"({deployment_runbook_check.get('detail')})"
        )
        if missing:
            lines.append("- missing_markers: " + ", ".join(str(item) for item in missing))
    readability_contract = report.get("review_readability_contract")
    lines.extend(["", "## Review Artifact Readability", ""])
    if not readability_contract:
        lines.append("- not checked")
    else:
        artifact = readability_contract.get("artifact") or {}
        lines.append(f"- ok: {str(readability_contract.get('ok')).lower()}")
        lines.append(f"- artifact: {artifact.get('path')}")
        lines.append(f"- audit_ok: {str(artifact.get('audit_ok')).lower()}")
        lines.append(f"- artifact_count: {artifact.get('artifact_count')}")
        lines.append(f"- finding_count: {artifact.get('finding_count')}")
        lines.append(f"- scoped_artifact_count: {artifact.get('scoped_artifact_count')}")
        lines.append(f"- scoped_finding_count: {artifact.get('scoped_finding_count')}")
        lines.append(f"- blocking_finding_count: {artifact.get('blocking_finding_count')}")
        lines.append(f"- status_update_allowed: {str(artifact.get('status_update_allowed')).lower()}")
        lines.append(f"- db_writes: {str(artifact.get('db_writes')).lower()}")
        lines.append(f"- approval_claim: {str(artifact.get('approval_claim')).lower()}")
        lines.append(f"- failure_count: {readability_contract.get('failure_count')}")
    demo_contract = report.get("demo_contract")
    lines.extend(["", "## AI-HR Demo Contract", ""])
    if not demo_contract:
        lines.append("- not checked")
    else:
        json_artifacts = demo_contract.get("json_artifacts") or []
        html_artifact = demo_contract.get("html_artifact") or {}
        lines.append(f"- ok: {str(demo_contract.get('ok')).lower()}")
        lines.append(f"- json_artifacts: {len(json_artifacts)}")
        lines.append(f"- html_artifact: {html_artifact.get('path') or 'none'}")
        lines.append(f"- failure_count: {demo_contract.get('failure_count')}")
        for artifact in json_artifacts:
            lines.append(
                "- "
                + f"{artifact.get('path')}: ok={artifact.get('ok')}, "
                + f"view={artifact.get('view')}, matrix_rows={artifact.get('matrix_rows')}"
            )
        if html_artifact:
            lines.append(
                "- "
                + f"{html_artifact.get('path')}: ok={html_artifact.get('ok')}, "
                + f"length={html_artifact.get('length')}"
            )
    dashboard_contract = report.get("dashboard_surface_contract")
    lines.extend(["", "## AI-HR Dashboard Surface", ""])
    if not dashboard_contract:
        lines.append("- not checked")
    else:
        artifact = dashboard_contract.get("artifact") or {}
        queue_summary = artifact.get("queue_status_summary") or {}
        review_chain_summary = artifact.get("review_chain_safety_summary") or {}
        live_summaries = artifact.get("live_plan_summaries") or []
        static_artifacts = artifact.get("static_artifacts") or []
        static_dates = artifact.get("static_artifact_dates") or []
        mixed_static_dates = artifact.get("mixed_static_artifact_dates") or False
        freshness_hash_skip_names = artifact.get("freshness_hash_skip_names") or []
        freshness_hash_skip_reason = (
            artifact.get("freshness_hash_skip_reason")
            if isinstance(artifact.get("freshness_hash_skip_reason"), dict)
            else {}
        )
        queue_status_artifact = next(
            (
                item
                for item in static_artifacts
                if isinstance(item, dict) and item.get("name") == "queue_status_json"
            ),
            {},
        )
        queue_run_artifact = next(
            (
                item
                for item in static_artifacts
                if isinstance(item, dict) and item.get("name") == "queue_run_json"
            ),
            {},
        )
        readiness_artifact = next(
            (
                item
                for item in static_artifacts
                if isinstance(item, dict) and item.get("name") == "readiness_json"
            ),
            {},
        )
        queue_status_snapshot = (
            queue_status_artifact.get("queue_status")
            if isinstance(queue_status_artifact.get("queue_status"), dict)
            else {}
        )
        queue_run_snapshot = (
            queue_run_artifact.get("queue_run")
            if isinstance(queue_run_artifact.get("queue_run"), dict)
            else {}
        )
        api_linkage_artifact = next(
            (
                item
                for item in static_artifacts
                if item.get("name") == "api_linkage_summary_json"
            ),
            {},
        )
        api_linkage_summary = (
            api_linkage_artifact.get("api_linkage_summary")
            if isinstance(api_linkage_artifact.get("api_linkage_summary"), dict)
            else {}
        )
        qualification_coverage_hint = (
            api_linkage_summary.get("qualification_coverage_plan_hint")
            if isinstance(api_linkage_summary.get("qualification_coverage_plan_hint"), dict)
            else {}
        )
        qualification_coverage_artifact = next(
            (
                item
                for item in static_artifacts
                if item.get("name") == "qualification_collection_coverage_plan_json"
            ),
            {},
        )
        qualification_coverage_summary = (
            qualification_coverage_artifact.get("qualification_collection_coverage_plan")
            if isinstance(
                qualification_coverage_artifact.get(
                    "qualification_collection_coverage_plan"
                ),
                dict,
            )
            else {}
        )
        release_readiness_snapshot = (
            readiness_artifact.get("release_readiness")
            if isinstance(readiness_artifact.get("release_readiness"), dict)
            else {}
        )
        lines.append(f"- ok: {str(dashboard_contract.get('ok')).lower()}")
        lines.append(f"- artifact: {artifact.get('path')}")
        lines.append(f"- scenario_count: {artifact.get('scenario_count')}")
        lines.append(f"- blocked_queue_items: {queue_summary.get('blocked_count')}")
        lines.append(f"- review_chain_contract_ok: {review_chain_summary.get('contract_ok')}")
        lines.append(f"- review_chain_source_payload_exposed: {review_chain_summary.get('source_payload_exposed')}")
        lines.append(f"- review_chain_learning_module_visible_items: {review_chain_summary.get('learning_module_visible_items')}")
        lines.append(f"- review_chain_ncs_report_visible_items: {review_chain_summary.get('ncs_report_visible_items')}")
        lines.append(f"- review_chain_ocr_context_card_count: {review_chain_summary.get('ocr_context_card_count')}")
        lines.append(f"- review_chain_blocked_automation_actions: {review_chain_summary.get('blocked_automation_actions')}")
        lines.append(f"- review_chain_issues: {review_chain_summary.get('issues')}")
        lines.append(f"- static_artifacts: {len(static_artifacts)}")
        lines.append(f"- static_artifact_dates: {static_dates}")
        lines.append(f"- mixed_static_artifact_dates: {mixed_static_dates}")
        lines.append(f"- freshness_hash_skip_names: {freshness_hash_skip_names}")
        if freshness_hash_skip_reason:
            for name in sorted(freshness_hash_skip_reason):
                lines.append(
                    "- "
                    + f"freshness_hash_skip_reason.{name}: "
                    + str(freshness_hash_skip_reason.get(name))
                )
        lines.append(
            "- release_agent_work_queue_path: "
            f"{release_readiness_snapshot.get('agent_work_queue_path')}"
        )
        lines.append(f"- queue_status_artifact_contract_ok: {queue_status_snapshot.get('contract_ok')}")
        lines.append(f"- queue_status_source_queue_path: {queue_status_snapshot.get('source_queue_path')}")
        lines.append(f"- queue_status_guarded_manual_items: {queue_status_snapshot.get('guarded_manual_items')}")
        lines.append(f"- queue_status_unsafe_manual_items: {queue_status_snapshot.get('unsafe_manual_items')}")
        lines.append(f"- queue_run_artifact_contract_ok: {queue_run_snapshot.get('contract_ok')}")
        lines.append(f"- queue_run_source_queue_path: {queue_run_snapshot.get('source_queue_path')}")
        lines.append(f"- queue_run_actual_run: {queue_run_snapshot.get('actual_run')}")
        lines.append(f"- queue_run_output_issues: {queue_run_snapshot.get('output_issues')}")
        if qualification_coverage_summary:
            lines.append(
                "- qualification_coverage_plan_path: "
                f"{qualification_coverage_artifact.get('path')}"
            )
            lines.append(
                "- qualification_coverage_plan_report_only: "
                f"{qualification_coverage_summary.get('report_only')}"
            )
            lines.append(
                "- qualification_coverage_plan_db_writes: "
                f"{qualification_coverage_summary.get('db_writes')}"
            )
            lines.append(
                "- qualification_coverage_plan_api_calls: "
                f"{qualification_coverage_summary.get('api_calls')}"
            )
            lines.append(
                "- qualification_coverage_plan_automatic_collection_allowed_now: "
                f"{qualification_coverage_summary.get('automatic_collection_allowed_now')}"
            )
            lines.append(
                "- qualification_coverage_plan_operator_only: "
                f"{qualification_coverage_summary.get('operator_timed_guarded_api_commands_only')}"
            )
            lines.append(
                "- qualification_coverage_plan_attempted_unit_count: "
                f"{qualification_coverage_summary.get('attempted_unit_count')}"
            )
            lines.append(
                "- qualification_coverage_plan_total_unit_count: "
                f"{qualification_coverage_summary.get('total_unit_count')}"
            )
            lines.append(
                "- qualification_coverage_plan_collection_coverage: "
                f"{qualification_coverage_summary.get('collection_coverage')}"
            )
            lines.append(
                "- qualification_coverage_plan_additional_attempted_units_needed: "
                f"{qualification_coverage_summary.get('additional_attempted_units_needed')}"
            )
            lines.append(
                "- qualification_coverage_plan_estimated_batch_count: "
                f"{qualification_coverage_summary.get('estimated_batch_count')}"
            )
            lines.append(
                "- qualification_coverage_plan_batch_count: "
                f"{qualification_coverage_summary.get('batch_count')}"
            )
            lines.append(
                "- qualification_coverage_plan_raw_batch_count_matches_batches: "
                f"{qualification_coverage_summary.get('raw_batch_count_matches_batches')}"
            )
            lines.append(
                "- qualification_coverage_plan_unsafe_batch_count: "
                f"{qualification_coverage_summary.get('unsafe_batch_count')}"
            )
            lines.append(
                "- qualification_coverage_plan_raw_unsafe_batch_count_matches_batches: "
                f"{qualification_coverage_summary.get('raw_unsafe_batch_count_matches_batches')}"
            )
            lines.append(
                "- qualification_coverage_plan_raw_unsafe_batches_count: "
                f"{qualification_coverage_summary.get('raw_unsafe_batches_count')}"
            )
            lines.append(
                "- qualification_coverage_plan_raw_unsafe_batches_match_batches: "
                f"{qualification_coverage_summary.get('raw_unsafe_batches_match_batches')}"
            )
            lines.append(
                "- qualification_coverage_plan_must_run_qualification_retry_hygiene_first: "
                f"{qualification_coverage_summary.get('must_run_qualification_retry_hygiene_first')}"
            )
            lines.append(
                "- qualification_coverage_plan_must_use_ncs006_checkpoint_path: "
                f"{qualification_coverage_summary.get('must_use_ncs006_checkpoint_path')}"
            )
            lines.append(
                "- qualification_coverage_plan_operator_timing_required: "
                f"{qualification_coverage_summary.get('operator_timing_required')}"
            )
            lines.append(
                "- qualification_coverage_plan_forbidden_status_updates_exact: "
                f"{qualification_coverage_summary.get('forbidden_status_updates_exact')}"
            )
            lines.append(
                "- qualification_coverage_hint_scope: "
                f"{qualification_coverage_hint.get('scope')}"
            )
            lines.append(
                "- qualification_coverage_hint_command_scope: "
                f"{qualification_coverage_hint.get('coverage_plan_command_scope')}"
            )
            lines.append(
                "- qualification_coverage_hint_matches_summary_scope: "
                f"{qualification_coverage_hint.get('coverage_plan_matches_summary_scope')}"
            )
            lines.append(
                "- qualification_coverage_hint_command_present: "
                f"{qualification_coverage_hint.get('coverage_plan_command_present')}"
            )
            lines.append(
                "- qualification_coverage_hint_global_command_present: "
                f"{qualification_coverage_hint.get('global_coverage_plan_command_present')}"
            )
        lines.append(f"- failure_count: {dashboard_contract.get('failure_count')}")
        for item in live_summaries:
            requested = item.get("requested_input") or {}
            necessity_summary = item.get("training_necessity_review_summary") or {}
            annual_summary = item.get("annual_operation_plan_summary") or {}
            lines.append(
                "- "
                + f"{item.get('name')}: ok={item.get('ok')}, "
                + f"{requested.get('current_query')} -> {requested.get('target_query')}, "
                + f"matrix_rows={item.get('matrix_rows')}, "
                + f"necessity_review_rows={necessity_summary.get('row_count')}, "
                + f"necessity_review_required={necessity_summary.get('review_required_rows')}, "
                + f"necessity_approval_blocked={necessity_summary.get('approval_blocked_rows')}, "
                + f"necessity_approval_claim_safe={necessity_summary.get('approval_claim_safe')}, "
                + f"annual_rows={annual_summary.get('row_count')}, "
                + f"annual_hours={annual_summary.get('estimated_total_hours')}, "
                + f"annual_pending={annual_summary.get('pending_human_decision_rows')}, "
                + f"annual_approval_claim_safe={annual_summary.get('approval_claim_safe')}, "
                + f"query_route_tool={item.get('query_route_tool')}, "
                + f"query_route_fingerprint={item.get('query_route_fingerprint')}, "
                + f"query_route_expected_tool_chain={item.get('query_route_expected_tool_chain')}, "
                + f"query_route_contract_fingerprint={item.get('query_route_contract_fingerprint')}, "
                + f"sensitive_markers={item.get('sensitive_markers')}"
            )
        if static_artifacts:
            lines.extend(["", "### Static Artifacts", ""])
            for item in static_artifacts:
                cycle_safe_sha256 = item.get("cycle_safe_content_sha256")
                cycle_safe_text = (
                    f", cycle_safe_content_sha256={cycle_safe_sha256}"
                    if cycle_safe_sha256
                    else ""
                )
                lines.append(
                    "- "
                    + f"{item.get('name')}: exists={item.get('exists')}, "
                    + f"non_empty={item.get('non_empty')}, "
                    + f"size_bytes={item.get('size_bytes')}, "
                    + f"content_sha256={item.get('content_sha256')}"
                    + cycle_safe_text
                    + ", "
                    + f"path={item.get('path')}"
                )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- `ok=true` means this readiness report was generated and its contracts were evaluated.",
            "- `engineering_hygiene_ok=true` means the release-readiness contract, artifact shape, and public tool boundary checks are green; executable test evidence must be read from the attached verification artifacts.",
            "- `release_ready=false` means data/review evidence is still insufficient for benchmark-grade release claims.",
            "- `approval_claim=false` means this report is not an operator approval and does not write review statuses.",
        ]
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an NCS MCP release-readiness report.")
    parser.add_argument("--quality-report", required=True, type=Path)
    parser.add_argument("--contract", default=ROOT / "mcp" / "ncs-tool-contract.json", type=Path)
    parser.add_argument("--demo-json", action="append", default=[], type=Path)
    parser.add_argument("--demo-html", type=Path)
    parser.add_argument("--dashboard-verification", type=Path)
    parser.add_argument("--review-priority-report", type=Path)
    parser.add_argument("--review-readability-audit", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--agent-queue-out", type=Path)
    parser.add_argument("--agent-queue-markdown-out", type=Path)
    parser.add_argument("--min-trusted-scenarios", type=int, default=10)
    parser.add_argument("--min-qualification-coverage", type=float, default=0.9)
    parser.add_argument("--fail-on-blockers", action="store_true")
    args = parser.parse_args(argv)

    quality_report = json.loads(args.quality_report.read_text(encoding="utf-8"))
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    quality_report_markdown_path = quality_report.get("markdown_path") or str(
        args.quality_report.with_suffix(".md")
    )
    review_priority_markdown_path = None
    if args.review_priority_report:
        review_priority_report = json.loads(args.review_priority_report.read_text(encoding="utf-8"))
        review_priority_markdown_path = review_priority_report.get("markdown_path") or str(
            args.review_priority_report.with_suffix(".md")
        )
    artifact_stamp = _artifact_stamp_from_paths(
        args.out,
        args.agent_queue_out,
        args.markdown_out,
        args.agent_queue_markdown_out,
        args.quality_report,
        args.contract,
        *args.demo_json,
        args.demo_html,
        args.dashboard_verification,
        args.review_readability_audit,
    )
    if args.agent_queue_out is None:
        args.agent_queue_out = _default_agent_queue_path(args.out, artifact_stamp)
    if args.agent_queue_markdown_out is None:
        args.agent_queue_markdown_out = args.agent_queue_out.with_suffix(".md")
    artifact_date = _artifact_date_from_paths(
        args.out,
        args.agent_queue_out,
        args.markdown_out,
        args.agent_queue_markdown_out,
        args.quality_report,
        args.contract,
        *args.demo_json,
        args.demo_html,
        args.dashboard_verification,
        args.review_readability_audit,
    )
    artifact_date_contract = {
        "release_outputs": _date_contract_for_paths(
            {
                "release_json": args.out,
                "release_markdown": args.markdown_out,
                "agent_queue_json": args.agent_queue_out,
                "agent_queue_markdown": args.agent_queue_markdown_out,
            },
            expected_date=artifact_date,
            expected_stamp=artifact_stamp,
        ),
        "proof_artifacts": _date_contract_for_paths(
            {
                "quality_report": args.quality_report,
                "contract": args.contract,
                "demo_json": args.demo_json,
                "demo_html": args.demo_html,
                "dashboard_verification": args.dashboard_verification,
                "review_priority_report": args.review_priority_report,
                "review_readability_audit": args.review_readability_audit,
            },
            expected_date=artifact_date,
            expected_stamp=artifact_stamp,
        ),
    }
    dashboard_surface_contract = build_dashboard_surface_contract(
        args.dashboard_verification,
        pending_release_readiness_path=args.out,
    )
    artifact_lineage_contract = _dashboard_verification_lineage_contract(
        dashboard_surface_contract,
        release_readiness_path=args.out,
        agent_queue_path=args.agent_queue_out,
    )
    readability_active_paths: list[Any] = [
        args.quality_report,
        args.contract,
        *args.demo_json,
        args.demo_html,
        args.dashboard_verification,
        args.review_priority_report,
    ]
    if dashboard_surface_contract:
        static_artifacts = (dashboard_surface_contract.get("artifact") or {}).get("static_artifacts")
        if isinstance(static_artifacts, list):
            readability_active_paths.extend(
                item.get("path")
                for item in static_artifacts
                if isinstance(item, dict) and item.get("path")
            )
    review_readability_contract = build_review_artifact_readability_contract(
        args.review_readability_audit,
        active_artifact_paths=readability_active_paths,
    )
    report = build_release_readiness(
        quality_report,
        contract,
        demo_contract=build_aihr_demo_contract(args.demo_json, args.demo_html),
        dashboard_surface_contract=dashboard_surface_contract,
        review_readability_contract=review_readability_contract,
        dashboard_static_artifact_dir=(
            args.dashboard_verification.parent if args.dashboard_verification else None
        ),
        quality_report_path=args.quality_report,
        quality_report_markdown_path=quality_report_markdown_path,
        release_readiness_markdown_path=args.markdown_out,
        review_priority_report_path=args.review_priority_report,
        review_priority_markdown_path=review_priority_markdown_path,
        min_trusted_scenarios=args.min_trusted_scenarios,
        min_qualification_coverage=args.min_qualification_coverage,
        artifact_date=artifact_stamp,
        artifact_date_contract=artifact_date_contract,
        artifact_lineage_contract=artifact_lineage_contract,
    )
    if args.markdown_out:
        report["markdown_path"] = str(args.markdown_out)
    report["agent_work_queue_path"] = str(args.agent_queue_out)
    report["agent_work_queue_markdown_path"] = str(args.agent_queue_markdown_out)
    _add_release_cycle_safe_hash_metadata(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown_out:
        write_markdown(report, args.markdown_out)
    if args.agent_queue_out:
        args.agent_queue_out.parent.mkdir(parents=True, exist_ok=True)
        args.agent_queue_out.write_text(
            json.dumps(report["agent_work_queue"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.agent_queue_markdown_out:
        write_agent_queue_markdown(report["agent_work_queue"], args.agent_queue_markdown_out)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.fail_on_blockers and report["blockers"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
