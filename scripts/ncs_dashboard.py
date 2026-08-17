from __future__ import annotations

import argparse
import copy
import getpass
import hashlib
import html
import ipaddress
import json
import os
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.error import URLError
from urllib.request import urlopen as urllib_urlopen


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
NCS_KNOWLEDGE_GRAPH_HTML = SCRIPTS / "ncs_knowledge_graph.html"
NCS_3D_FORCE_GRAPH_JS = SCRIPTS / "vendor" / "3d-force-graph-1.80.0.min.js"
READONLY_REFRESH_REPORTS = ROOT / "reports" / "overnight_sessions" / "readonly_refresh"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ncs_mcp.config import load_settings
from ncs_mcp.contracts import PLAN_NCS_EDUCATION_PATH_TOOL, QUERY_ROUTE_SCHEMA
from ncs_mcp.db import (
    connect,
    ensure_ontology_seeded,
    initialize_database,
    ksa_label_quality_flags,
    normalize_concept_key,
    now_utc,
)
from ncs_mcp.ksa_label_report import build_ksa_label_auto_triage_report
from ncs_mcp.knowledge_graph import (
    KnowledgeGraphDataError,
    build_ncs_knowledge_graph,
)
from ncs_mcp import tool_registry
from ncs_mcp.agent_queue import build_agent_queue_status_from_file
from ncs_mcp.query_router import aihr_plan_route_evidence, route_ncs_query
from ncs_mcp.release_labels import (
    blocker_display_label,
    blocker_display_labels,
    blocker_display_message,
)
from ncs_mcp.refinement import apply_refinement_to_target
from ncs_mcp.review_safety import (
    resolve_repo_reports_artifact,
    review_packet_sha256 as shared_review_packet_sha256,
)
from ncs_mcp.training_recommendation import (
    compact_ncs_education_plan_response,
    recommend_training_transition,
)
from render_aihr_plan_demo import public_demo_payload


def default_dashboard_reviewer_id() -> str:
    raw_user = (
        os.environ.get("USERNAME")
        or os.environ.get("USER")
        or getpass.getuser()
        or ""
    )
    reviewer = re.sub(r"[^A-Za-z0-9_.@-]+", "_", raw_user).strip("._-")
    if reviewer.lower() in DASHBOARD_AUTOMATED_REVIEWER_IDS:
        return ""
    return reviewer

_DB_PREPARE_LOCK = Lock()
_DB_SCHEMA_PREPARED_PATHS: set[Path] = set()
_DB_ONTOLOGY_PREPARED_PATHS: set[Path] = set()
_DASHBOARD_SCHEMA_READY_TABLES = (
    "classifications",
    "competency_units",
    "competency_elements",
    "quality_issues",
    "sqf_duties",
)
_DASHBOARD_ONTOLOGY_READY_TABLES = (
    "ontology_concepts",
    "ontology_concept_aliases",
    "ksa_concept_links",
)
KSA_DEFINITION_DASHBOARD_TABLES = (
    "classifications",
    "competency_units",
    "competency_elements",
    "ksa_items",
    "ksa_atomic_items",
    "ksa_atomic_concept_links",
    "ksa_concept_links",
    "ontology_concepts",
    "ontology_concept_aliases",
    "ontology_concept_label_candidates",
    "criteria_concept_links",
    "ksa_meaning_candidates",
)
KSA_LABEL_PATTERN_TABLES = (
    "classifications",
    "competency_units",
    "competency_elements",
    "ksa_items",
    "ontology_concept_label_candidates",
    "review_audit_log",
)
KSA_LABEL_AUTO_TRIAGE_TABLES = (
    "classifications",
    "competency_units",
    "competency_elements",
    "ksa_items",
    "ksa_atomic_items",
    "ksa_atomic_concept_links",
    "ksa_concept_links",
    "ontology_concepts",
    "ontology_concept_label_candidates",
    "review_audit_log",
)
KSA_LABEL_PATTERN_HUMAN_STATUSES = ("human_reviewed", "accepted", "reviewed")
KSA_LABEL_FORBIDDEN_STATUS_OVERRIDE_FIELDS = (
    "review_status",
    "new_status",
    "target_review_status",
    "proposed_review_status",
    "requested_review_status",
)
KSA_LABEL_FORBIDDEN_APPROVAL_PAYLOAD_FIELDS = (
    "approval_claim",
    "human_reviewed",
    "accepted",
    "reviewed",
)
KSA_LABEL_PATTERN_DOMAIN_MAJOR_CODES = ("14", "15", "16", "17", "19", "21", "23", "24")
KSA_LABEL_PATTERN_GENERIC_LABELS = (
    "관리",
    "검토",
    "계획",
    "기획",
    "분석",
    "수행",
    "운영",
    "이해",
    "작성",
    "점검",
    "조사",
    "처리",
    "평가",
    "확인",
    "활용",
)
DASHBOARD_LOOKUP_TABLES = (
    "classifications",
    "competency_units",
    "competency_elements",
    "performance_criteria",
    "ksa_items",
)
DASHBOARD_ONTOLOGY_LOOKUP_TABLES = tuple(
    dict.fromkeys(
        (
            *DASHBOARD_LOOKUP_TABLES,
            *_DASHBOARD_ONTOLOGY_READY_TABLES,
            "ontology_concept_relations",
            "criteria_concept_links",
        )
    )
)
API_ORPHAN_LOOKUP_TABLES = (
    "api_competency_units",
    "competency_units",
)
AIHR_DEMO_HTML_GLOB = "aihr_plan_demo*.html"
AIHR_READINESS_JSON_GLOB = "aihr_release_readiness*.json"
AIHR_REVIEW_TRIAGE_JSON_GLOB = "aihr_review_triage*.json"
AIHR_AGENT_QUEUE_JSON_GLOB = "aihr_agent_queue*.json"
AIHR_AGENT_WORK_QUEUE_JSON_GLOB = "aihr_agent_work_queue*.json"
AIHR_AGENT_QUEUE_JSON_GLOBS = (AIHR_AGENT_QUEUE_JSON_GLOB, AIHR_AGENT_WORK_QUEUE_JSON_GLOB)
AIHR_AGENT_QUEUE_STATUS_JSON_GLOB = "aihr_agent_queue_status*.json"
AIHR_AGENT_QUEUE_RUN_JSON_GLOB = "aihr_agent_queue_run_20*.json"
AIHR_PROVENANCE_RECONFIRMATION_JSON_GLOBS = (
    "aihr_human_review_provenance_reconfirmation_packet_20*.json",
    "human_review_provenance_reconfirmation_packet_20*.json",
)
REVIEW_SEEDPACK_JSONL_GLOBS = (
    "aihr_review_seedpack*.jsonl",
    "review_seedpack*.jsonl",
)
KSA_LABEL_NEEDS_REVIEW_HTML_GLOB = "ksa_label_needs_review_seedpack_20*.html"
KSA_MEANING_NEEDS_REVIEW_HTML_GLOB = "ksa_meaning_needs_review_seedpack_20*.html"
KSA_MEANING_MISSING_SCOPED_HTML_GLOB = "ksa_meaning_missing_scoped_seedpack_20*.html"
KSA_PREPROCESSING_PIPELINE_HTML_GLOB = "ksa_preprocessing_pipeline_status_20*.html"
KSA_TERM_REVIEW_READINESS_JSON_GLOB = "ksa_term_review_operator_packet*_readiness.json"
KSA_HUMAN_REVIEW_BACKLOG_JSON_GLOB = "human_review_backlog*.json"
KSA_DEFINITION_PROMOTION_STATUS_JSON_GLOBS = (
    "ksa_definition_review_operator_packet*_promotion_status.json",
    "ksa_definition_promotion_status*.json",
)
KSA_DEFINITION_CANDIDATE_FAMILY_JSON_GLOB = "ksa_definition_candidate_family_report*.json"
KSA_SHORT_LABEL_FAMILY_JSON_GLOB = "ksa_short_label_family_report*.json"
KSA_SHORT_LABEL_PATTERN_JSON_GLOB = "ksa_short_label_pattern_report*.json"
KSA_SHORT_LABEL_FAMILY_CANONICAL_JSON = "ksa_short_label_family_report_20260626.json"
KSA_SHORT_LABEL_PATTERN_CANONICAL_JSON = "ksa_short_label_pattern_report_20260626.json"
KSA_LLM_PREPROCESSING_BACKLOG_JSON_GLOB = "llm_preprocessing_backlog_map_20*.json"
KSA_LLM_PREPROCESSING_WORK_PLAN_JSON_GLOB = "llm_preprocessing_next_8h_work_plan_20*.json"
QUERY_ROUTER_SAMPLES: tuple[dict[str, str], ...] = (
    {
        "label": "Education-system transition",
        "query": "노무관리에서 인사기획으로 교육훈련체계 로드맵",
    },
    {
        "label": "Official-claim risk",
        "query": "인사기획 훈련과정을 정부 공식 승인 또는 자격 인정 근거로 볼 수 있나요?",
    },
    {
        "label": "Operator review gated route",
        "query": "review quality issue for training goal link",
    },
)


class DashboardReadOnlyError(RuntimeError):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        missing_tables: list[str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.missing_tables = missing_tables or []

    def to_payload(self) -> dict:
        payload = {"code": self.code, "detail": self.detail}
        if self.missing_tables:
            payload["missing_tables"] = self.missing_tables
        return payload


def resolve_aihr_demo_html_path(reports_dir: Path | None = None) -> Path | None:
    configured = os.environ.get("NCS_AIHR_DEMO_HTML_PATH")
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.exists() else None
    base_dir = reports_dir or (ROOT / "reports")
    candidates = [path for path in base_dir.glob(AIHR_DEMO_HTML_GLOB) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=_dated_artifact_sort_key)


def _dated_artifact_sort_key(path: Path) -> tuple[int, float]:
    for part in reversed(path.stem.split("_")):
        if len(part) == 8 and part.isdigit():
            return int(part), path.stat().st_mtime
    return 0, path.stat().st_mtime


def resolve_aihr_readiness_json_path(reports_dir: Path | None = None) -> Path | None:
    configured = os.environ.get("NCS_AIHR_READINESS_JSON_PATH")
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.exists() else None
    base_dir = reports_dir or (ROOT / "reports")
    candidates = [path for path in base_dir.glob(AIHR_READINESS_JSON_GLOB) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=_dated_artifact_sort_key)


def resolve_aihr_review_triage_json_path(reports_dir: Path | None = None) -> Path | None:
    configured = os.environ.get("NCS_AIHR_REVIEW_TRIAGE_JSON_PATH")
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.exists() else None
    base_dir = reports_dir or (ROOT / "reports")
    candidates = [path for path in base_dir.glob(AIHR_REVIEW_TRIAGE_JSON_GLOB) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=_dated_artifact_sort_key)


def _resolve_existing_report_artifact_path(
    value: object,
    *,
    reports_dir: Path,
    anchor_path: Path | None = None,
) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    raw_path = Path(text)
    candidates: list[Path] = []
    if raw_path.is_absolute():
        candidates.append(raw_path)
    else:
        candidates.append(reports_dir / raw_path)
        candidates.append(reports_dir / raw_path.name)
        if anchor_path is not None:
            candidates.append(anchor_path.parent / raw_path)
            candidates.append(anchor_path.parent / raw_path.name)
        candidates.append(ROOT / raw_path)
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _read_latest_aihr_readiness_payload(
    reports_dir: Path,
) -> tuple[Path, dict] | tuple[None, None]:
    readiness_path = resolve_aihr_readiness_json_path(reports_dir)
    if readiness_path is None:
        return None, None
    try:
        payload = json.loads(readiness_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    return readiness_path, payload


def _readiness_declared_artifact_path(
    *,
    reports_dir: Path,
    direct_field: str | None = None,
    static_artifact_name: str | None = None,
) -> Path | None:
    declared, path = _readiness_declared_artifact_resolution(
        reports_dir=reports_dir,
        direct_field=direct_field,
        static_artifact_name=static_artifact_name,
    )
    return path if declared else None


def _readiness_declared_artifact_resolution(
    *,
    reports_dir: Path,
    direct_field: str | None = None,
    static_artifact_name: str | None = None,
) -> tuple[bool, Path | None]:
    readiness_path, payload = _read_latest_aihr_readiness_payload(reports_dir)
    if readiness_path is None or payload is None:
        return False, None
    if direct_field:
        direct_value = payload.get(direct_field)
        if str(direct_value or "").strip():
            direct_path = _resolve_existing_report_artifact_path(
                direct_value,
                reports_dir=reports_dir,
                anchor_path=readiness_path,
            )
            return True, direct_path
        direct_path = _resolve_existing_report_artifact_path(
            direct_value,
            reports_dir=reports_dir,
            anchor_path=readiness_path,
        )
        if direct_path is not None:
            return True, direct_path
    if static_artifact_name:
        dashboard_contract = payload.get("dashboard_surface_contract")
        artifact = (
            dashboard_contract.get("artifact")
            if isinstance(dashboard_contract, dict)
            and isinstance(dashboard_contract.get("artifact"), dict)
            else {}
        )
        static_artifacts = artifact.get("static_artifacts")
        if isinstance(static_artifacts, list):
            for item in static_artifacts:
                if not isinstance(item, dict) or item.get("name") != static_artifact_name:
                    continue
                static_path = _resolve_existing_report_artifact_path(
                    item.get("path"),
                    reports_dir=reports_dir,
                    anchor_path=readiness_path,
                )
                return True, static_path
    return False, None


def resolve_aihr_provenance_reconfirmation_json_path(
    reports_dir: Path | None = None,
) -> Path | None:
    configured = os.environ.get("NCS_AIHR_PROVENANCE_RECONFIRMATION_JSON_PATH")
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.exists() else None
    base_dir = reports_dir or (ROOT / "reports")
    candidate_dirs = [base_dir]
    readonly_dir = base_dir / "overnight_sessions" / "readonly_refresh"
    if readonly_dir not in candidate_dirs:
        candidate_dirs.append(readonly_dir)
    if reports_dir is None and READONLY_REFRESH_REPORTS not in candidate_dirs:
        candidate_dirs.append(READONLY_REFRESH_REPORTS)
    candidates: list[Path] = []
    seen: set[Path] = set()
    for directory in candidate_dirs:
        for glob in AIHR_PROVENANCE_RECONFIRMATION_JSON_GLOBS:
            for path in directory.glob(glob):
                if path.is_file() and path not in seen:
                    seen.add(path)
                    candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=_dated_artifact_sort_key)


def resolve_review_seedpack_jsonl_path(reports_dir: Path | None = None) -> Path | None:
    configured = os.environ.get("NCS_REVIEW_SEEDPACK_PATH")
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.exists() else None
    base_dir = reports_dir or (ROOT / "reports")
    candidates: list[Path] = []
    for glob in REVIEW_SEEDPACK_JSONL_GLOBS:
        candidates.extend(path for path in base_dir.glob(glob) if path.is_file())
    if not candidates:
        return None
    return max(candidates, key=_dated_artifact_sort_key)


def resolve_ksa_review_html_path(glob: str, reports_dir: Path | None = None) -> Path | None:
    base_dir = reports_dir or (ROOT / "reports")
    candidates = [path for path in base_dir.glob(glob) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=_dated_artifact_sort_key)


def _resolve_latest_report_json_path(
    globs: str | tuple[str, ...],
    reports_dir: Path | None = None,
) -> Path | None:
    base_dir = reports_dir or (ROOT / "reports")
    patterns = (globs,) if isinstance(globs, str) else globs
    candidates: list[Path] = []
    for glob in patterns:
        candidates.extend(path for path in base_dir.glob(glob) if path.is_file())
    if not candidates:
        return None
    return max(candidates, key=_dated_artifact_sort_key)


def _resolve_preferred_report_json_path(
    preferred_name: str,
    glob: str,
    reports_dir: Path | None = None,
) -> Path | None:
    base_dir = reports_dir or (ROOT / "reports")
    preferred_path = base_dir / preferred_name
    if preferred_path.is_file():
        return preferred_path
    return _resolve_latest_report_json_path(glob, reports_dir)


def resolve_ksa_term_review_readiness_json_path(reports_dir: Path | None = None) -> Path | None:
    configured = os.environ.get("NCS_KSA_TERM_REVIEW_READINESS_JSON_PATH")
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.exists() else None
    return _resolve_latest_report_json_path(KSA_TERM_REVIEW_READINESS_JSON_GLOB, reports_dir)


def resolve_ksa_human_review_backlog_json_path(reports_dir: Path | None = None) -> Path | None:
    configured = os.environ.get("NCS_KSA_HUMAN_REVIEW_BACKLOG_JSON_PATH")
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.exists() else None
    return _resolve_latest_report_json_path(KSA_HUMAN_REVIEW_BACKLOG_JSON_GLOB, reports_dir)


def resolve_ksa_definition_promotion_status_json_path(
    reports_dir: Path | None = None,
) -> Path | None:
    configured = os.environ.get("NCS_KSA_DEFINITION_PROMOTION_STATUS_JSON_PATH")
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.exists() else None
    return _resolve_latest_report_json_path(KSA_DEFINITION_PROMOTION_STATUS_JSON_GLOBS, reports_dir)


def resolve_ksa_definition_candidate_family_json_path(
    reports_dir: Path | None = None,
) -> Path | None:
    configured = os.environ.get("NCS_KSA_DEFINITION_CANDIDATE_FAMILY_JSON_PATH")
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.exists() else None
    return _resolve_latest_report_json_path(KSA_DEFINITION_CANDIDATE_FAMILY_JSON_GLOB, reports_dir)


def resolve_ksa_short_label_family_json_path(
    reports_dir: Path | None = None,
) -> Path | None:
    configured = os.environ.get("NCS_KSA_SHORT_LABEL_FAMILY_JSON_PATH")
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.exists() else None
    return _resolve_preferred_report_json_path(
        KSA_SHORT_LABEL_FAMILY_CANONICAL_JSON,
        KSA_SHORT_LABEL_FAMILY_JSON_GLOB,
        reports_dir,
    )


def resolve_ksa_short_label_pattern_json_path(
    reports_dir: Path | None = None,
) -> Path | None:
    configured = os.environ.get("NCS_KSA_SHORT_LABEL_PATTERN_JSON_PATH")
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.exists() else None
    return _resolve_preferred_report_json_path(
        KSA_SHORT_LABEL_PATTERN_CANONICAL_JSON,
        KSA_SHORT_LABEL_PATTERN_JSON_GLOB,
        reports_dir,
    )


def resolve_ksa_llm_preprocessing_backlog_json_path(
    reports_dir: Path | None = None,
) -> Path | None:
    configured = os.environ.get("NCS_KSA_LLM_PREPROCESSING_BACKLOG_JSON_PATH")
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.exists() else None
    return _resolve_latest_report_json_path(
        KSA_LLM_PREPROCESSING_BACKLOG_JSON_GLOB,
        reports_dir,
    )


def resolve_ksa_llm_preprocessing_work_plan_json_path(
    reports_dir: Path | None = None,
) -> Path | None:
    configured = os.environ.get("NCS_KSA_LLM_PREPROCESSING_WORK_PLAN_JSON_PATH")
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.exists() else None
    return _resolve_latest_report_json_path(
        KSA_LLM_PREPROCESSING_WORK_PLAN_JSON_GLOB,
        reports_dir,
    )


def _read_report_json(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_report_json_with_error(path: Path | None) -> tuple[dict, dict | None]:
    if path is None:
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return {}, {
            "code": "malformed_json_input",
            "path": str(path),
            "line": exc.lineno,
            "column": exc.colno,
            "message": exc.msg,
        }
    except UnicodeDecodeError as exc:
        return {}, {
            "code": "invalid_utf8_input",
            "path": str(path),
            "start": exc.start,
            "end": exc.end,
            "message": exc.reason,
        }
    except OSError as exc:
        return {}, {
            "code": "report_read_error",
            "path": str(path),
            "message": str(exc),
        }
    if not isinstance(payload, dict):
        return {}, {
            "code": "json_root_not_object",
            "path": str(path),
            "message": "Report JSON root must be an object.",
        }
    return payload, None


def _dashboard_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _dashboard_bool_or_none(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _first_dashboard_bool_or_none(*values: object) -> bool | None:
    for value in values:
        parsed = _dashboard_bool_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _get_nested_dict(payload: dict, key: str) -> dict:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _compact_first_review_queue(rows: object, *, limit: int = 8) -> list[dict]:
    if not isinstance(rows, list):
        return []
    compact: list[dict] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        compact.append(
            {
                "concept_id": row.get("concept_id"),
                "concept_name": row.get("concept_name"),
                "concept_type": row.get("concept_type"),
                "suggested_decision": row.get("suggested_decision"),
                "suggested_decision_confidence": row.get("suggested_decision_confidence"),
                "max_priority_score": _dashboard_int(
                    row.get("max_priority_score") or row.get("minimal_review_priority_score")
                ),
                "item_count": _dashboard_int(row.get("item_count")),
                "task_relation_count": _dashboard_int(row.get("task_relation_count")),
                "training_course_link_count": _dashboard_int(row.get("training_course_link_count")),
            }
        )
    return compact


def _compact_definition_family_queue(rows: object, *, limit: int = 8) -> list[dict]:
    if not isinstance(rows, list):
        return []
    compact: list[dict] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        samples = row.get("samples") if isinstance(row.get("samples"), list) else []
        risk_samples = row.get("risk_samples") if isinstance(row.get("risk_samples"), list) else []
        sample = samples[0] if samples and isinstance(samples[0], dict) else {}
        risk_sample = risk_samples[0] if risk_samples and isinstance(risk_samples[0], dict) else {}
        compact.append(
            {
                "family_key": row.get("family_key"),
                "family_label": row.get("family_label"),
                "concept_type": row.get("concept_type"),
                "candidate_count": _dashboard_int(row.get("candidate_count")),
                "risk_count": _dashboard_int(row.get("risk_count")),
                "risk_flag_counts": row.get("risk_flag_counts")
                if isinstance(row.get("risk_flag_counts"), dict)
                else {},
                "recommended_review_level": row.get("recommended_review_level"),
                "sample_meaning_text": sample.get("meaning_text"),
                "risk_sample_meaning_text": risk_sample.get("meaning_text"),
                "risk_sample_flags": risk_sample.get("risk_flags")
                if isinstance(risk_sample.get("risk_flags"), list)
                else [],
            }
        )
    return compact


def _compact_label_family_queue(rows: object, *, limit: int = 8) -> list[dict]:
    if not isinstance(rows, list):
        return []
    compact: list[dict] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        samples = row.get("samples") if isinstance(row.get("samples"), list) else []
        risk_samples = row.get("risk_samples") if isinstance(row.get("risk_samples"), list) else []
        sample = samples[0] if samples and isinstance(samples[0], dict) else {}
        risk_sample = risk_samples[0] if risk_samples and isinstance(risk_samples[0], dict) else {}
        compact.append(
            {
                "family_key": row.get("family_key"),
                "representative_label": row.get("representative_label"),
                "concept_type": row.get("concept_type"),
                "row_count": _dashboard_int(row.get("row_count")),
                "concept_count": _dashboard_int(row.get("concept_count")),
                "scope_count": _dashboard_int(row.get("scope_count")),
                "risk_score": _dashboard_int(row.get("risk_score")),
                "risk_level": row.get("risk_level"),
                "risk_reasons": row.get("risk_reasons")
                if isinstance(row.get("risk_reasons"), list)
                else [],
                "review_status_counts": row.get("review_status_counts")
                if isinstance(row.get("review_status_counts"), dict)
                else {},
                "quality_flag_counts": row.get("quality_flag_counts")
                if isinstance(row.get("quality_flag_counts"), dict)
                else {},
                "sample_source_text": sample.get("source_text"),
                "risk_sample_source_text": risk_sample.get("source_text"),
            }
        )
    return compact


def _compact_label_pattern_queue(rows: object, *, limit: int = 8) -> list[dict]:
    if not isinstance(rows, list):
        return []
    compact: list[dict] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        samples = row.get("samples") if isinstance(row.get("samples"), list) else []
        sample = samples[0] if samples and isinstance(samples[0], dict) else {}
        compact.append(
            {
                "pattern_key": row.get("pattern_key"),
                "pattern_name": row.get("pattern_name"),
                "concept_type": row.get("concept_type"),
                "row_count": _dashboard_int(row.get("row_count")),
                "concept_count": _dashboard_int(row.get("concept_count")),
                "label_family_count": _dashboard_int(row.get("label_family_count")),
                "collision_label_family_count": _dashboard_int(
                    row.get("collision_label_family_count")
                ),
                "max_collision_concept_count": _dashboard_int(
                    row.get("max_collision_concept_count")
                ),
                "collision_risk_hint": row.get("collision_risk_hint"),
                "scope_count": _dashboard_int(row.get("scope_count")),
                "risk_score": _dashboard_int(row.get("risk_score")),
                "risk_level": row.get("risk_level"),
                "automation_recommendation": row.get("automation_recommendation"),
                "minimum_review_unit": row.get("minimum_review_unit"),
                "operator_decision_hint": row.get("operator_decision_hint"),
                "decision_options": row.get("decision_options")
                if isinstance(row.get("decision_options"), list)
                else [],
                "recommended_handling": row.get("recommended_handling"),
                "quality_flag_counts": row.get("quality_flag_counts")
                if isinstance(row.get("quality_flag_counts"), dict)
                else {},
                "sample_source_text": sample.get("source_text"),
                "sample_label_text": sample.get("label_text"),
                "sample_method_details": sample.get("method_details"),
                "sample_removed_char_count": _dashboard_int(
                    sample.get("short_label_removed_char_count")
                ),
                "sample_length_ratio": sample.get("short_label_length_ratio"),
                "sample_collision_risk": sample.get("collision_risk"),
                "sample_label_family_pattern_concept_count": _dashboard_int(
                    sample.get("label_family_pattern_concept_count")
                ),
            }
        )
    return compact


def _compact_llm_preprocessing_backlog(
    payload: dict,
    path: Path | None,
    read_error: dict | None = None,
) -> dict:
    if read_error:
        return {
            "available": True,
            "path": str(path) if path else None,
            "safety_ok": False,
            "read_error": read_error,
            "source_issue_count": 1,
        }
    if not payload:
        return {
            "available": False,
            "path": str(path) if path else None,
            "safety_ok": False,
        }
    summary = _get_nested_dict(payload, "summary")
    review_status_policy = _get_nested_dict(payload, "review_status_policy")
    policy_snapshot = _get_nested_dict(payload, "policy_snapshot")
    auto_triage = _get_nested_dict(policy_snapshot, "auto_triage")
    sampling_plan = _get_nested_dict(policy_snapshot, "sampling_plan")
    safety_ok = (
        payload.get("schema") == "ncs_llm_preprocessing_backlog_map_v1"
        and payload.get("ok") is True
        and payload.get("report_only") is True
        and payload.get("status_update_allowed") is False
        and payload.get("db_writes") is False
        and payload.get("approval_claim") is False
        and payload.get("human_decision_required_for_approval") is True
        and review_status_policy.get("human_decision_required_for_status_update") is True
        and "human_reviewed" in (review_status_policy.get("forbidden_automatic_statuses") or [])
        and "accepted" in (review_status_policy.get("forbidden_automatic_statuses") or [])
        and "reviewed" in (review_status_policy.get("forbidden_automatic_statuses") or [])
        and not payload.get("source_issues")
    )
    return {
        "available": True,
        "path": str(path) if path else None,
        "schema": payload.get("schema"),
        "ok": payload.get("ok"),
        "status": payload.get("status"),
        "safety_ok": safety_ok,
        "report_only": payload.get("report_only"),
        "status_update_allowed": payload.get("status_update_allowed"),
        "db_writes": payload.get("db_writes"),
        "approval_claim": payload.get("approval_claim"),
        "human_decision_required_for_approval": _dashboard_bool_or_none(
            payload.get("human_decision_required_for_approval")
        ),
        "blocker_count": _dashboard_int(payload.get("blocker_count")),
        "source_issue_count": len(payload.get("source_issues") or []),
        "raw_ksa_rows": _dashboard_int(summary.get("raw_ksa_rows")),
        "label_candidate_rows": _dashboard_int(summary.get("label_candidate_rows")),
        "human_reviewed_label_rows": _dashboard_int(
            summary.get("human_reviewed_label_rows")
        ),
        "pending_label_rows_not_trusted": _dashboard_int(
            summary.get("pending_label_rows_not_trusted")
        ),
        "distinct_normalized_label_keys": _dashboard_int(
            summary.get("distinct_normalized_label_keys")
        ),
        "distinct_concepts_with_label_candidates": _dashboard_int(
            summary.get("distinct_concepts_with_label_candidates")
        ),
        "ontology_concepts": _dashboard_int(summary.get("ontology_concepts")),
        "ontology_concepts_human_reviewed": _dashboard_int(
            summary.get("ontology_concepts_human_reviewed")
        ),
        "meaning_candidate_rows": _dashboard_int(summary.get("meaning_candidate_rows")),
        "task_ksa_concept_relation_rows": _dashboard_int(
            summary.get("task_ksa_concept_relation_rows")
        ),
        "training_goal_concept_link_rows": _dashboard_int(
            summary.get("training_goal_concept_link_rows")
        ),
        "training_course_concept_link_rows": _dashboard_int(
            summary.get("training_course_concept_link_rows")
        ),
        "non_approval_statuses": review_status_policy.get("non_approval_statuses")
        if isinstance(review_status_policy.get("non_approval_statuses"), list)
        else [],
        "auto_triage_classification_counts": auto_triage.get("classification_v2_counts")
        if isinstance(auto_triage.get("classification_v2_counts"), dict)
        else {},
        "recommended_sample_rows_total": _dashboard_int(
            sampling_plan.get("recommended_sample_rows_total")
        ),
        "estimated_click_reduction_ratio": sampling_plan.get(
            "estimated_click_reduction_ratio"
        ),
    }


def _compact_llm_preprocessing_work_plan(
    payload: dict,
    path: Path | None,
    read_error: dict | None = None,
) -> dict:
    if read_error:
        return {
            "available": True,
            "path": str(path) if path else None,
            "safety_ok": False,
            "read_error": read_error,
            "source_issue_count": 1,
        }
    if not payload:
        return {
            "available": False,
            "path": str(path) if path else None,
            "safety_ok": False,
        }
    safety_contract = _get_nested_dict(payload, "safety_contract")
    artifact_policy = _get_nested_dict(payload, "artifact_policy")
    input_summary = _get_nested_dict(payload, "input_summary")
    forbidden_statuses = safety_contract.get("forbidden_automatic_statuses") or []
    safety_ok = (
        payload.get("schema") == "ncs_llm_preprocessing_work_plan_v1"
        and payload.get("ok") is True
        and payload.get("report_only") is True
        and payload.get("status_update_allowed") is False
        and payload.get("db_writes") is False
        and payload.get("approval_claim") is False
        and payload.get("human_decision_required_for_approval") is True
        and safety_contract.get("trusted_status_write_allowed") is False
        and safety_contract.get("raw_source_mutation_allowed") is False
        and "human_reviewed" in forbidden_statuses
        and "accepted" in forbidden_statuses
        and "reviewed" in forbidden_statuses
        and artifact_policy.get("db_apply_allowed") is False
        and artifact_policy.get("guarded_collection_allowed") is False
        and artifact_policy.get("operator_decision_fields_auto_filled") is False
        and not payload.get("source_issues")
    )
    tracks = []
    for row in payload.get("work_tracks") or []:
        if not isinstance(row, dict):
            continue
        tracks.append(
            {
                "priority": row.get("priority"),
                "track": row.get("track"),
                "input_rows": _dashboard_int(row.get("input_rows")),
                "human_gate": row.get("human_gate"),
            }
        )
    return {
        "available": True,
        "path": str(path) if path else None,
        "schema": payload.get("schema"),
        "ok": payload.get("ok"),
        "status": payload.get("status"),
        "safety_ok": safety_ok,
        "report_only": payload.get("report_only"),
        "status_update_allowed": payload.get("status_update_allowed"),
        "db_writes": payload.get("db_writes"),
        "approval_claim": payload.get("approval_claim"),
        "human_decision_required_for_approval": _dashboard_bool_or_none(
            payload.get("human_decision_required_for_approval")
        ),
        "next_action": payload.get("next_action"),
        "source_backlog_map": payload.get("source_backlog_map"),
        "source_artifact_hash": payload.get("source_artifact_hash"),
        "track_count": len(tracks),
        "tracks": tracks[:8],
        "label_candidate_rows": _dashboard_int(input_summary.get("label_candidate_rows")),
        "recommended_sample_rows_total": _dashboard_int(
            input_summary.get("recommended_sample_rows_total")
        ),
        "estimated_click_reduction_ratio": input_summary.get(
            "estimated_click_reduction_ratio"
        ),
        "trusted_status_write_allowed": _dashboard_bool_or_none(
            safety_contract.get("trusted_status_write_allowed")
        ),
        "raw_source_mutation_allowed": _dashboard_bool_or_none(
            safety_contract.get("raw_source_mutation_allowed")
        ),
        "forbidden_automatic_statuses": forbidden_statuses
        if isinstance(forbidden_statuses, list)
        else [],
        "db_apply_allowed": _dashboard_bool_or_none(
            artifact_policy.get("db_apply_allowed")
        ),
        "guarded_collection_allowed": _dashboard_bool_or_none(
            artifact_policy.get("guarded_collection_allowed")
        ),
        "operator_decision_fields_auto_filled": _dashboard_bool_or_none(
            artifact_policy.get("operator_decision_fields_auto_filled")
        ),
        "source_issue_count": len(payload.get("source_issues") or []),
    }


def get_ksa_preprocessing_review_status(reports_dir: Path | None = None) -> dict:
    base_dir = reports_dir or (ROOT / "reports")
    readiness_path = resolve_ksa_term_review_readiness_json_path(base_dir)
    readiness = _read_report_json(readiness_path)
    workflow_path = _resolve_existing_report_artifact_path(
        readiness.get("workflow_manifest_path"),
        reports_dir=base_dir,
        anchor_path=readiness_path,
    )
    workflow = _read_report_json(workflow_path)
    workflow_summary = _get_nested_dict(workflow, "summary")
    readiness_summary = _get_nested_dict(readiness, "summary")
    summary = {**workflow_summary, **readiness_summary}

    backlog_path = resolve_ksa_human_review_backlog_json_path(base_dir)
    backlog = _read_report_json(backlog_path)
    seedpack_safety = _get_nested_dict(backlog, "seedpack_safety")
    definition_packet_safety = _get_nested_dict(
        seedpack_safety,
        "ksa_definition_review_operator_packet",
    )
    definition_packet_sidecar_safety = _get_nested_dict(
        definition_packet_safety,
        "sidecar_safety",
    )

    promotion_path = resolve_ksa_definition_promotion_status_json_path(base_dir)
    promotion = _read_report_json(promotion_path)
    label_family_path = resolve_ksa_short_label_family_json_path(base_dir)
    label_family_report = _read_report_json(label_family_path)
    label_pattern_path = resolve_ksa_short_label_pattern_json_path(base_dir)
    label_pattern_report = _read_report_json(label_pattern_path)
    definition_family_path = resolve_ksa_definition_candidate_family_json_path(base_dir)
    definition_family_report = _read_report_json(definition_family_path)
    llm_backlog_path = resolve_ksa_llm_preprocessing_backlog_json_path(base_dir)
    llm_backlog_report, llm_backlog_read_error = _read_report_json_with_error(
        llm_backlog_path
    )
    llm_work_plan_path = resolve_ksa_llm_preprocessing_work_plan_json_path(base_dir)
    llm_work_plan_report, llm_work_plan_read_error = _read_report_json_with_error(
        llm_work_plan_path
    )

    first_review_queue = readiness.get("first_review_queue") or summary.get("first_review_queue") or []
    suggested_counts = _get_nested_dict(summary, "suggested_decision_counts")
    pending_decision_count = _dashboard_int(
        summary.get("pending_decision_count") or readiness.get("pending_decision_count")
    )
    concept_review_group_count = _dashboard_int(summary.get("concept_review_group_count"))
    action_count = _dashboard_int(readiness.get("action_count") or summary.get("action_count"))

    minimal_review = {
        "available": bool(readiness),
        "ready_for_minimal_human_review": bool(readiness.get("ready_for_minimal_human_review")),
        "ready_for_guarded_action_plan_review": bool(
            readiness.get("ready_for_guarded_action_plan_review")
        ),
        "next_step": readiness.get("next_step") or "generate_ksa_term_review_operator_packet",
        "concept_review_group_count": concept_review_group_count,
        "pending_decision_count": pending_decision_count,
        "decision_blank_count": _dashboard_int(summary.get("decision_blank_count")),
        "completed_decision_count": _dashboard_int(summary.get("completed_decision_count")),
        "action_count": action_count,
        "minimal_review_item_count": _dashboard_int(summary.get("minimal_review_item_count")),
        "suggested_decision_counts": suggested_counts,
        "suggested_decision_confidence_counts": _get_nested_dict(
            summary,
            "suggested_decision_confidence_counts",
        ),
        "first_review_queue": _compact_first_review_queue(first_review_queue),
    }

    safety_contract = _get_nested_dict(workflow, "safety_contract")
    safety = {
        "status_update_allowed": _first_dashboard_bool_or_none(
            readiness.get("status_update_allowed"),
            workflow.get("status_update_allowed"),
            label_pattern_report.get("status_update_allowed"),
            label_family_report.get("status_update_allowed"),
            definition_family_report.get("status_update_allowed"),
        ),
        "db_writes": _first_dashboard_bool_or_none(
            readiness.get("db_writes"),
            workflow.get("db_writes"),
            label_pattern_report.get("db_writes"),
            label_family_report.get("db_writes"),
            definition_family_report.get("db_writes"),
        ),
        "approval_claim": _first_dashboard_bool_or_none(
            readiness.get("approval_claim"),
            workflow.get("approval_claim"),
            label_pattern_report.get("approval_claim"),
            label_family_report.get("approval_claim"),
            definition_family_report.get("approval_claim"),
        ),
        "raw_source_mutation_allowed": _dashboard_bool_or_none(
            readiness.get(
                "raw_source_mutation_allowed",
                safety_contract.get("raw_source_mutation_allowed"),
            )
        ),
        "trusted_status_write_allowed": _dashboard_bool_or_none(
            readiness.get(
                "trusted_status_write_allowed",
                safety_contract.get("trusted_status_write_allowed"),
            )
        ),
    }
    safety_surface_safe = all(value is not True for value in safety.values())

    backlog_summary = {
        "available": bool(backlog),
        "all_seedpacks_safe": bool(seedpack_safety.get("all_seedpacks_safe")),
        "total_review_items": _dashboard_int(seedpack_safety.get("total_review_items")),
        "total_nonblank_decision_items": _dashboard_int(
            seedpack_safety.get("total_nonblank_decision_items")
        ),
        "total_trusted_status_proposals": _dashboard_int(
            seedpack_safety.get("total_trusted_status_proposals")
        ),
        "total_status_update_allowed_violations": _dashboard_int(
            seedpack_safety.get("total_status_update_allowed_violations")
        ),
        "ksa_definition_review_operator_packet": {
            "available": bool(definition_packet_safety),
            "safety_ok": _dashboard_bool_or_none(definition_packet_safety.get("safety_ok")),
            "source_payload_exposed": _dashboard_bool_or_none(
                definition_packet_safety.get("source_payload_exposed")
            ),
            "status_update_allowed": _dashboard_bool_or_none(
                definition_packet_safety.get("status_update_allowed")
            ),
            "db_writes": _dashboard_bool_or_none(definition_packet_safety.get("db_writes")),
            "approval_claim": _dashboard_bool_or_none(
                definition_packet_safety.get("approval_claim")
            ),
            "trusted_status_write_allowed": _dashboard_bool_or_none(
                definition_packet_safety.get("trusted_status_write_allowed")
            ),
            "raw_source_mutation_allowed": _dashboard_bool_or_none(
                definition_packet_safety.get("raw_source_mutation_allowed")
            ),
            "review_pack_row_count": _dashboard_int(
                definition_packet_safety.get("review_pack_row_count")
            ),
            "decision_blank_count": _dashboard_int(
                definition_packet_safety.get("decision_blank_count")
            ),
            "pending_decision_count": _dashboard_int(
                definition_packet_safety.get("pending_decision_count")
            ),
            "completed_decision_count": _dashboard_int(
                definition_packet_safety.get("completed_decision_count")
            ),
            "sidecar_safety_ok": _dashboard_bool_or_none(
                definition_packet_sidecar_safety.get("safety_ok")
            ),
            "sidecar_consistency_issues": definition_packet_sidecar_safety.get(
                "consistency_issues"
            )
            if isinstance(definition_packet_sidecar_safety.get("consistency_issues"), list)
            else [],
        },
        "blockers": backlog.get("blockers") if isinstance(backlog.get("blockers"), list) else [],
    }

    definition_promotion = {
        "available": bool(promotion),
        "ok": bool(promotion.get("ok")),
        "candidate_rows_scanned": _dashboard_int(promotion.get("candidate_rows_scanned")),
        "promotable": _dashboard_int(promotion.get("promotable")),
        "skipped_boilerplate": _dashboard_int(promotion.get("skipped_boilerplate")),
        "skipped_human_lock": _dashboard_int(promotion.get("skipped_human_lock")),
        "skipped_boilerplate_by_concept_type": _get_nested_dict(
            promotion,
            "skipped_boilerplate_by_concept_type",
        ),
        "promotable_by_concept_type": _get_nested_dict(promotion, "promotable_by_concept_type"),
    }
    definition_family = {
        "available": bool(definition_family_report),
        "ok": bool(definition_family_report.get("ok")),
        "candidate_count": _dashboard_int(definition_family_report.get("candidate_count")),
        "definition_family_count": _dashboard_int(
            definition_family_report.get("definition_family_count")
        ),
        "estimated_review_unit_count": _dashboard_int(
            definition_family_report.get("estimated_review_unit_count")
        ),
        "risk_candidate_count": _dashboard_int(definition_family_report.get("risk_candidate_count")),
        "risk_flag_family_count": _dashboard_int(
            definition_family_report.get("risk_flag_family_count")
        ),
        "row_to_estimated_review_unit_reduction_percent": float(
            definition_family_report.get("row_to_estimated_review_unit_reduction_percent") or 0.0
        ),
        "review_unit_model": definition_family_report.get("review_unit_model"),
        "review_status_counts": _get_nested_dict(definition_family_report, "review_status_counts"),
        "risk_flag_counts": _get_nested_dict(definition_family_report, "risk_flag_counts"),
        "top_families": _compact_definition_family_queue(
            definition_family_report.get("top_families"),
        ),
    }
    label_family = {
        "available": bool(label_family_report),
        "ok": bool(label_family_report.get("ok")),
        "candidate_count": _dashboard_int(label_family_report.get("candidate_count")),
        "label_family_count": _dashboard_int(label_family_report.get("label_family_count")),
        "risk_label_family_count": _dashboard_int(
            label_family_report.get("risk_label_family_count")
        ),
        "emitted_first_pass_family_count": _dashboard_int(
            label_family_report.get("emitted_first_pass_family_count")
        ),
        "estimated_first_pass_review_unit_count": _dashboard_int(
            label_family_report.get("estimated_first_pass_review_unit_count")
        ),
        "row_to_first_pass_reduction_percent": float(
            label_family_report.get("row_to_first_pass_reduction_percent") or 0.0
        ),
        "review_unit_model": label_family_report.get("review_unit_model"),
        "review_status_counts": _get_nested_dict(label_family_report, "review_status_counts"),
        "quality_flag_counts": _get_nested_dict(label_family_report, "quality_flag_counts"),
        "risk_level_counts": _get_nested_dict(label_family_report, "risk_level_counts"),
        "top_families": _compact_label_family_queue(label_family_report.get("top_families")),
    }
    label_pattern = {
        "available": bool(label_pattern_report),
        "ok": bool(label_pattern_report.get("ok")),
        "candidate_count": _dashboard_int(label_pattern_report.get("candidate_count")),
        "pattern_count": _dashboard_int(label_pattern_report.get("pattern_count")),
        "emitted_pattern_count": _dashboard_int(label_pattern_report.get("emitted_pattern_count")),
        "estimated_first_pass_review_unit_count": _dashboard_int(
            label_pattern_report.get("estimated_first_pass_review_unit_count")
        ),
        "row_to_first_pass_reduction_percent": float(
            label_pattern_report.get("row_to_first_pass_reduction_percent") or 0.0
        ),
        "review_unit_model": label_pattern_report.get("review_unit_model"),
        "concept_type_counts": _get_nested_dict(label_pattern_report, "concept_type_counts"),
        "quality_flag_counts": _get_nested_dict(label_pattern_report, "quality_flag_counts"),
        "top_patterns": _compact_label_pattern_queue(label_pattern_report.get("top_patterns")),
    }
    llm_preprocessing_backlog = _compact_llm_preprocessing_backlog(
        llm_backlog_report,
        llm_backlog_path,
        llm_backlog_read_error,
    )
    llm_preprocessing_work_plan = _compact_llm_preprocessing_work_plan(
        llm_work_plan_report,
        llm_work_plan_path,
        llm_work_plan_read_error,
    )
    llm_backlog_safe_for_status = (
        not llm_preprocessing_backlog["available"]
        or bool(llm_preprocessing_backlog["safety_ok"])
    )
    llm_work_plan_safe_for_status = (
        not llm_preprocessing_work_plan["available"]
        or bool(llm_preprocessing_work_plan["safety_ok"])
    )
    review_backlog_safe_for_status = (
        not backlog_summary["available"]
        or bool(backlog_summary["all_seedpacks_safe"])
    )
    has_preprocessing_status_artifact = bool(
        readiness
        or backlog
        or promotion
        or definition_family_report
        or label_family_report
        or label_pattern_report
        or (
            llm_preprocessing_backlog["available"]
            and llm_preprocessing_backlog["safety_ok"]
        )
        or (
            llm_preprocessing_work_plan["available"]
            and llm_preprocessing_work_plan["safety_ok"]
        )
    )

    ontology_next_actions = []
    if concept_review_group_count:
        ontology_next_actions.append(
            {
                "priority": "P0",
                "title": "첫 휴먼리뷰를 concept group 단위로 제한",
                "detail": (
                    f"{concept_review_group_count}개 그룹만 먼저 확인합니다. "
                    f"CSV 결정 대기 {pending_decision_count}건, 완료 {action_count}건입니다."
                ),
                "evidence": str(readiness_path or ""),
            }
        )
    if suggested_counts:
        ontology_next_actions.append(
            {
                "priority": "P0",
                "title": "범용 KSA term은 추천 점수 반영 전에 downweight 후보로 검토",
                "detail": (
                    "suggested_decision은 검토 보조일 뿐이며 자동 결정이 아닙니다. "
                    f"분포: {suggested_counts}"
                ),
                "evidence": str(workflow_path or readiness_path or ""),
            }
        )
    if definition_promotion["available"]:
        ontology_next_actions.append(
            {
                "priority": "P1",
                "title": "boilerplate 정의는 ontology_concepts.definition으로 승격 금지",
                "detail": (
                    f"후보 {definition_promotion['candidate_rows_scanned']:,}건 중 "
                    f"승격 가능 {definition_promotion['promotable']:,}건, "
                    f"boilerplate 제외 {definition_promotion['skipped_boilerplate']:,}건입니다."
                ),
                "evidence": str(promotion_path or ""),
            }
        )
    if definition_family["available"]:
        ontology_next_actions.append(
            {
                "priority": "P0",
                "title": "정의 문장 후보는 개별 클릭이 아니라 정의 패밀리 단위로 확인",
                "detail": (
                    f"후보 {definition_family['candidate_count']:,}건을 "
                    f"{definition_family['estimated_review_unit_count']:,}개 리뷰 단위로 축소했습니다. "
                    f"축소율 {definition_family['row_to_estimated_review_unit_reduction_percent']}%입니다."
                ),
                "evidence": str(definition_family_path or ""),
            }
        )
    if label_family["available"]:
        ontology_next_actions.append(
            {
                "priority": "P0",
                "title": "단어형 라벨 후보는 행 클릭이 아니라 라벨 패밀리 first-pass 큐로 확인",
                "detail": (
                    f"라벨 후보 {label_family['candidate_count']:,}건을 "
                    f"{label_family['estimated_first_pass_review_unit_count']:,}개 first-pass 리뷰 단위로 축소했습니다. "
                    f"축소율 {label_family['row_to_first_pass_reduction_percent']}%입니다."
                ),
                "evidence": str(label_family_path or ""),
            }
        )
    if label_pattern["available"]:
        ontology_next_actions.append(
            {
                "priority": "P1",
                "title": "needs_review 라벨은 행이 아니라 변환 패턴 단위로 확인",
                "detail": (
                    f"needs_review {label_pattern['candidate_count']:,}건을 "
                    f"{label_pattern['estimated_first_pass_review_unit_count']:,}개 패턴 리뷰 단위로 축소했습니다. "
                    f"축소율 {label_pattern['row_to_first_pass_reduction_percent']}%입니다."
                ),
                "evidence": str(label_pattern_path or ""),
            }
        )
    if backlog_summary["available"]:
        ontology_next_actions.append(
            {
                "priority": "P1",
                "title": "리뷰팩은 빈 결정지로 유지하고 감사 후에만 action plan 검토",
                "detail": (
                    f"리뷰 항목 {backlog_summary['total_review_items']:,}건, "
                    f"비어 있지 않은 결정 {backlog_summary['total_nonblank_decision_items']:,}건, "
                    f"status_update_allowed 위반 {backlog_summary['total_status_update_allowed_violations']:,}건입니다."
                ),
                "evidence": str(backlog_path or ""),
            }
        )

    return {
        "schema": "ncs_ksa_preprocessing_review_status_v1",
        "ok": bool(
            has_preprocessing_status_artifact
            and safety_surface_safe
            and review_backlog_safe_for_status
            and llm_backlog_safe_for_status
            and llm_work_plan_safe_for_status
        ),
        "generated_from": "reports_artifacts_read_only",
        "source_paths": {
            "readiness": str(readiness_path) if readiness_path else None,
            "workflow_manifest": str(workflow_path) if workflow_path else None,
            "human_review_backlog": str(backlog_path) if backlog_path else None,
            "definition_promotion_status": str(promotion_path) if promotion_path else None,
            "definition_candidate_family": str(definition_family_path)
            if definition_family_path
            else None,
            "short_label_family": str(label_family_path) if label_family_path else None,
            "short_label_pattern": str(label_pattern_path) if label_pattern_path else None,
            "llm_preprocessing_backlog": str(llm_backlog_path)
            if llm_backlog_path
            else None,
            "llm_preprocessing_work_plan": str(llm_work_plan_path)
            if llm_work_plan_path
            else None,
        },
        "minimal_review": minimal_review,
        "safety": {**safety, "safety_ok": safety_surface_safe},
        "backlog": backlog_summary,
        "definition_promotion": definition_promotion,
        "definition_family": definition_family,
        "label_family": label_family,
        "label_pattern": label_pattern,
        "llm_preprocessing_backlog": llm_preprocessing_backlog,
        "llm_preprocessing_work_plan": llm_preprocessing_work_plan,
        "ontology_next_actions": ontology_next_actions,
    }


def resolve_aihr_agent_queue_json_path(reports_dir: Path | None = None) -> Path | None:
    configured = os.environ.get("NCS_AIHR_AGENT_QUEUE_JSON_PATH")
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.exists() else None
    base_dir = reports_dir or (ROOT / "reports")
    readiness_declared, readiness_path = _readiness_declared_artifact_resolution(
        reports_dir=base_dir,
        direct_field="agent_work_queue_path",
    )
    if readiness_declared:
        return readiness_path
    candidates: list[tuple[int, int, int, float, Path]] = []
    for glob in AIHR_AGENT_QUEUE_JSON_GLOBS:
        for path in base_dir.glob(glob):
            if not path.is_file():
                continue
            stem = path.stem
            is_current = stem.startswith("aihr_agent_queue_") or stem == "aihr_agent_queue"
            is_legacy = stem.startswith("aihr_agent_work_queue_") or stem == "aihr_agent_work_queue"
            if not is_current and not is_legacy:
                continue
            prefix = "aihr_agent_queue" if is_current else "aihr_agent_work_queue"
            suffix = stem.removeprefix(prefix)
            artifact_date = 0
            stamped = 0
            if suffix:
                match = re.match(r"^_(\d{8})(?:_.+)?$", suffix)
                if not match:
                    continue
                artifact_date = int(match.group(1))
                stamped = 1
            if not suffix:
                artifact_date = 0
            if artifact_date == 0 and suffix:
                continue
            current_priority = 1 if is_current else 0
            candidates.append((artifact_date, current_priority, stamped, path.stat().st_mtime, path))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[:4])[4]


def aihr_agent_queue_expected_globs() -> list[str]:
    return [
        _public_aihr_path_text(ROOT / "reports" / glob)
        for glob in AIHR_AGENT_QUEUE_JSON_GLOBS
    ]


def resolve_aihr_agent_queue_status_json_path(reports_dir: Path | None = None) -> Path | None:
    configured = os.environ.get("NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH")
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.exists() else None
    base_dir = reports_dir or (ROOT / "reports")
    readiness_declared, readiness_path = _readiness_declared_artifact_resolution(
        reports_dir=base_dir,
        static_artifact_name="queue_status_json",
    )
    if readiness_declared:
        return readiness_path
    candidates = [path for path in base_dir.glob(AIHR_AGENT_QUEUE_STATUS_JSON_GLOB) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=_dated_artifact_sort_key)


def resolve_aihr_agent_queue_run_json_path(reports_dir: Path | None = None) -> Path | None:
    configured = os.environ.get("NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH")
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.exists() else None
    base_dir = reports_dir or (ROOT / "reports")
    readiness_declared, readiness_path = _readiness_declared_artifact_resolution(
        reports_dir=base_dir,
        static_artifact_name="queue_run_json",
    )
    if readiness_declared:
        return readiness_path
    candidates = [path for path in base_dir.glob(AIHR_AGENT_QUEUE_RUN_JSON_GLOB) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=_dated_artifact_sort_key)


AIHR_AGENT_QUEUE_EXECUTED_STATUSES = {"succeeded", "failed", "failed_timeout"}


def is_actual_aihr_agent_queue_run(payload: dict, source_path: Path | None = None) -> bool:
    if source_path is not None and "dryrun" in source_path.stem.lower():
        return False
    if payload.get("schema") != "aihr_agent_queue_run_v1":
        return False
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    if summary.get("dry_run") is True:
        return False
    if summary.get("dry_run_count", 0):
        return False
    runs = payload.get("runs")
    if not isinstance(runs, list):
        return False
    return any(
        isinstance(run, dict) and run.get("status") in AIHR_AGENT_QUEUE_EXECUTED_STATUSES
        for run in runs
    )


def _canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _resolved_aihr_agent_queue_path(source_queue_path: Any) -> Path | None:
    text = str(source_queue_path or "").strip()
    if not text:
        return None
    resolved_path = Path(text)
    if not resolved_path.is_absolute():
        resolved_path = ROOT / resolved_path
    return resolved_path


def aihr_agent_queue_run_artifact_issues(payload: dict, source_path: Path) -> list[str]:
    issues: list[str] = []
    if source_path.suffix.lower() == ".json" and source_path.stem.endswith("_public"):
        private_path = source_path.with_name(
            source_path.stem[: -len("_public")] + source_path.suffix
        )
        if private_path.exists():
            try:
                if private_path.stat().st_mtime > source_path.stat().st_mtime:
                    issues.append("public_stale_private_newer")
            except OSError:
                issues.append("public_private_sync_unreadable")
    source_queue_path = payload.get("source_queue_path")
    declared_hash = str(payload.get("source_queue_sha256") or "").strip().lower()
    queue_status_hash = str(
        payload.get("queue_status_snapshot_sha256") or ""
    ).strip().lower()
    resolved_queue_path = _resolved_aihr_agent_queue_path(source_queue_path)
    if not source_queue_path:
        issues.append("source_queue_path_missing")
    elif not declared_hash:
        issues.append("source_queue_sha256_missing")
    elif not re.fullmatch(r"sha256:[0-9a-f]{64}", declared_hash):
        issues.append("source_queue_sha256_invalid")
    else:
        if not resolved_queue_path.exists():
            issues.append("source_queue_missing")
        else:
            try:
                current_hash = "sha256:" + hashlib.sha256(
                    resolved_queue_path.read_bytes()
                ).hexdigest()
            except OSError:
                issues.append("source_queue_unreadable")
            else:
                if current_hash != declared_hash:
                    issues.append("source_queue_hash_mismatch")
    if not queue_status_hash:
        issues.append("queue_status_snapshot_sha256_missing")
    elif not re.fullmatch(r"sha256:[0-9a-f]{64}", queue_status_hash):
        issues.append("queue_status_snapshot_sha256_invalid")
    elif resolved_queue_path is None:
        issues.append("queue_status_snapshot_source_queue_path_missing")
    elif not resolved_queue_path.exists():
        issues.append("queue_status_snapshot_source_queue_missing")
    else:
        try:
            current_status = build_agent_queue_status_from_file(
                resolved_queue_path,
                workspace=ROOT,
            )
            current_status_hash = _canonical_json_sha256(current_status)
        except ValueError:
            issues.append("queue_status_snapshot_unreadable")
        else:
            if current_status_hash != queue_status_hash:
                issues.append("queue_status_snapshot_sha256_mismatch")
    return issues


def sanitize_aihr_agent_queue_run_payload(payload: dict) -> dict:
    sanitized = copy.deepcopy(payload)
    sanitize_aihr_agent_queue_public_paths(sanitized)
    sanitized["output_tails_suppressed"] = True
    runs = sanitized.get("runs")
    if not isinstance(runs, list):
        return sanitized
    for item in runs:
        if not isinstance(item, dict):
            continue
        item["command_label"] = safe_aihr_agent_queue_run_command_label(item)
        item.pop("command", None)
        for prefix in ("stdout", "stderr"):
            item.pop(f"{prefix}_tail", None)
            item[f"{prefix}_tail_suppressed"] = True
    return sanitized


AIHR_AGENT_QUEUE_PUBLIC_PATH_KEYS = {
    "checkpoint_path",
    "path",
    "source_path",
    "source_queue_path",
}
AIHR_DASHBOARD_PUBLIC_PATH_KEYS = AIHR_AGENT_QUEUE_PUBLIC_PATH_KEYS | {
    "artifact_path",
    "csv_path",
    "database_path",
    "db_path",
    "html_path",
    "json_path",
    "local_db_path",
    "markdown_path",
    "source_database_path",
}
AIHR_DASHBOARD_PUBLIC_PATH_MAP_KEYS = {"source_paths"}
AIHR_DASHBOARD_PUBLIC_PATH_LIST_KEYS = {
    "expected_artifacts",
    "expected_globs",
    "existing_expected_artifacts",
    "missing_expected_artifacts",
    "missing_prerequisite_artifacts",
    "prerequisite_artifacts",
}
AIHR_DASHBOARD_PUBLIC_COMMAND_KEYS = {"command"}
AIHR_DASHBOARD_PUBLIC_COMMAND_LIST_KEYS = {"prerequisite_commands"}
AIHR_DASHBOARD_WORKSPACE_KEYS = {
    "project_root",
    "root_path",
    "workspace",
    "workspace_path",
}
AIHR_LOCAL_DATABASE_REF = "configured_ncs_database"
AIHR_WORKSPACE_REF = "configured_workspace"


def _normalized_public_path_text(value: object) -> str:
    return str(value or "").strip().replace("\\", "/")


def _looks_like_windows_absolute_path(text: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", text)) or text.startswith("\\\\")


AIHR_REPOSITORY_PATH_MARKERS = (
    "/reports/",
    "/data/",
    "/scripts/",
    "/src/",
    "/docs/",
    "/tests/",
    "/.agents/",
)


def _inferred_aihr_workspace_roots(text: str) -> set[str]:
    normalized = _normalized_public_path_text(text).rstrip("/")
    lowered = normalized.lower()
    roots: set[str] = set()
    for marker in AIHR_REPOSITORY_PATH_MARKERS:
        marker_index = lowered.find(marker)
        if marker_index > 0:
            roots.add(normalized[:marker_index])
    return roots


def _relative_to_dashboard_root_text(text: str) -> str | None:
    normalized = _normalized_public_path_text(text).rstrip("/")
    if not normalized:
        return None
    root_candidates = {
        _normalized_public_path_text(ROOT).rstrip("/"),
        _normalized_public_path_text(ROOT.resolve()).rstrip("/"),
        *_inferred_aihr_workspace_roots(normalized),
    }
    for root_text in root_candidates:
        if not root_text:
            continue
        if normalized.lower() == root_text.lower():
            return ""
        prefix = f"{root_text}/"
        if normalized.lower().startswith(prefix.lower()):
            return normalized[len(prefix) :]
    return None


def _looks_like_local_database_path(value: object) -> bool:
    text = _normalized_public_path_text(value).lower()
    if not text:
        return False
    if "/data/processed/" not in f"/{text}":
        return False
    return bool(
        re.search(r"/data/processed/[^/]+\.db(?:-(?:wal|shm|journal))?$", f"/{text}")
    )


def _public_aihr_path_text(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    if _looks_like_local_database_path(text):
        return AIHR_LOCAL_DATABASE_REF
    relative_text = _relative_to_dashboard_root_text(text)
    if relative_text is not None:
        return relative_text or AIHR_WORKSPACE_REF
    if _looks_like_windows_absolute_path(text):
        return PureWindowsPath(text).name
    path = Path(text)
    if not path.is_absolute():
        return _normalized_public_path_text(text)
    try:
        return path.resolve(strict=False).relative_to(ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


_AIHR_COMMAND_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<path>[A-Za-z]:[\\/][^\s\"'<>|;]+|\\\\[^\s\"'<>|;]+)"
)
_AIHR_COMMAND_LOCAL_DB_RE = re.compile(
    r"(?P<path>(?:^|(?<=[\s\"']))data[\\/]processed[\\/][^\s\"'<>|;]+\.db"
    r"(?:-(?:wal|shm|journal))?)",
    re.IGNORECASE,
)


def _public_aihr_command_text(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""

    def replace_absolute(match: re.Match[str]) -> str:
        return _public_aihr_path_text(match.group("path"))

    text = _AIHR_COMMAND_ABSOLUTE_PATH_RE.sub(replace_absolute, text)

    def replace_local_db(match: re.Match[str]) -> str:
        return _public_aihr_path_text(match.group("path"))

    return _AIHR_COMMAND_LOCAL_DB_RE.sub(replace_local_db, text)


def sanitize_aihr_public_paths(payload: dict) -> dict:
    def visit(value: object) -> None:
        if isinstance(value, dict):
            for workspace_key in AIHR_DASHBOARD_WORKSPACE_KEYS:
                if workspace_key in value:
                    value.pop(workspace_key, None)
                    value.setdefault("workspace_ref", AIHR_WORKSPACE_REF)
            for key, child in list(value.items()):
                if key in AIHR_DASHBOARD_PUBLIC_PATH_KEYS and isinstance(child, str):
                    value[key] = _public_aihr_path_text(child)
                elif key in AIHR_DASHBOARD_PUBLIC_COMMAND_KEYS and isinstance(child, str):
                    value[key] = _public_aihr_command_text(child)
                elif key in AIHR_DASHBOARD_PUBLIC_PATH_MAP_KEYS and isinstance(child, dict):
                    for path_key, path_value in list(child.items()):
                        if isinstance(path_value, str):
                            child[path_key] = _public_aihr_path_text(path_value)
                        else:
                            visit(path_value)
                elif key in AIHR_DASHBOARD_PUBLIC_PATH_LIST_KEYS and isinstance(child, list):
                    sanitized_children: list[object] = []
                    for path_value in child:
                        if isinstance(path_value, str):
                            sanitized_children.append(_public_aihr_path_text(path_value))
                        else:
                            visit(path_value)
                            sanitized_children.append(path_value)
                    value[key] = sanitized_children
                elif key in AIHR_DASHBOARD_PUBLIC_COMMAND_LIST_KEYS and isinstance(child, list):
                    sanitized_children = []
                    for command_value in child:
                        if isinstance(command_value, str):
                            sanitized_children.append(_public_aihr_command_text(command_value))
                        else:
                            visit(command_value)
                            sanitized_children.append(command_value)
                    value[key] = sanitized_children
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return payload


def sanitize_aihr_agent_queue_public_paths(payload: dict) -> dict:
    return sanitize_aihr_public_paths(payload)


def public_aihr_dashboard_payload(payload: dict) -> dict:
    return sanitize_aihr_public_paths(copy.deepcopy(payload))


def safe_aihr_agent_queue_run_command_label(item: dict) -> str:
    existing = item.get("command_label")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    command = str(item.get("command") or "")
    match = re.search(r"(?:^|\s)(?:scripts[\\/])?ncs_harness\.py\s+([A-Za-z0-9_-]+)", command)
    if match:
        return f"ncs_harness:{match.group(1)}"
    return str(item.get("id") or item.get("owner") or "command_redacted")


def _html_escape(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _join_route_values(values: object, *, key: str | None = None) -> str:
    if not values:
        return "none"
    if isinstance(values, list):
        parts: list[str] = []
        for value in values:
            if key and isinstance(value, dict):
                parts.append(str(value.get(key) or ""))
            elif isinstance(value, dict) and {"code", "severity"} <= set(value):
                parts.append(f"{value.get('code')} ({value.get('severity')})")
            else:
                parts.append(str(value))
        return ", ".join(part for part in parts if part) or "none"
    return str(values)


def get_query_router_samples() -> list[dict[str, object]]:
    routes: list[dict[str, object]] = []
    public_tools = tool_registry.mcp_tools_for_mode(operator_tools_enabled=False)
    for sample in QUERY_ROUTER_SAMPLES:
        route = route_ncs_query(sample["query"], available_tool_names=public_tools)
        routes.append({"label": sample["label"], **route})
    return routes


def render_query_router_samples_html(routes: list[dict[str, object]] | None = None) -> str:
    route_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(route.get('label'))}<br><span class=\"muted\">{_html_escape(route.get('query'))}</span></td>"
        f"<td>{_html_escape(route.get('scenario'))}</td>"
        f"<td>{_html_escape(route.get('tool'))}</td>"
        f"<td>{_html_escape(route.get('confidence'))}</td>"
        f"<td>{_html_escape(_join_route_values(route.get('missing_params')))}</td>"
        f"<td>{_html_escape(route.get('available'))}</td>"
        f"<td>{_html_escape(_join_route_values(route.get('matched_signals')))}</td>"
        f"<td>{_html_escape(_join_route_values(route.get('pipeline'), key='tool'))}</td>"
        f"<td>{_html_escape(_join_route_values(route.get('expected_tool_chain')))}</td>"
        f"<td>{_html_escape(_join_route_values(route.get('guard_flags')))}</td>"
        f"<td>{_html_escape(_join_route_values(route.get('risk_flags')))}</td>"
        f"<td><code>{_html_escape(route.get('route_fingerprint'))}</code></td>"
        "</tr>"
        for route in (routes or get_query_router_samples())
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NCS Query Router Samples</title>
  <style>
    :root {{ --ink:#172033; --muted:#667085; --line:#d9e0ea; --bg:#f6f8fb; --panel:#fff; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Segoe UI, Arial, "Malgun Gothic", sans-serif; background:var(--bg); color:var(--ink); }}
    main {{ max-width:1320px; margin:0 auto; padding:24px; }}
    h1 {{ margin:0 0 6px; font-size:26px; }}
    h2 {{ margin:22px 0 8px; font-size:18px; }}
    .muted {{ color:var(--muted); }}
    table {{ width:100%; border-collapse:collapse; background:var(--panel); table-layout:fixed; margin-top:16px; }}
    th, td {{ border:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; overflow-wrap:anywhere; font-size:13px; }}
    th {{ background:#eef2f5; }}
    @media (max-width:760px) {{ main {{ padding:12px; }} table {{ display:block; overflow-x:auto; }} th, td {{ min-width:150px; }} }}
  </style>
</head>
<body>
<main>
  <h1>NCS Query Router Samples</h1>
  <p class="muted">Read-only Law-MCP-style routing examples generated from src/ncs_mcp/query_router.py. This page does not open the SQLite DB or execute recommendation logic.</p>
  <table>
    <thead><tr><th>Sample Intent</th><th>Scenario</th><th>Tool</th><th>Confidence</th><th>Missing Params</th><th>Available</th><th>Matched Signals</th><th>Pipeline</th><th>Expected Tool Chain</th><th>Guard Flags</th><th>Risk Flags</th><th>Route Fingerprint</th></tr></thead>
    <tbody>{route_rows or '<tr><td colspan="12">none</td></tr>'}</tbody>
  </table>
</main>
</body>
</html>
"""


def _payload_text(payload: dict, key: str, default: str = "") -> str:
    value = payload.get(key, default)
    return str(value if value is not None else "").strip()


def _payload_float(payload: dict, key: str) -> float | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _payload_int(payload: dict, key: str, default: int, minimum: int = 1, maximum: int = 10) -> int:
    try:
        return max(minimum, min(int(payload.get(key, default)), maximum))
    except (TypeError, ValueError):
        return default


def _payload_methods(payload: dict) -> list[str]:
    value = payload.get("preferred_methods")
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _payload_facilities(payload: dict) -> list[str]:
    value = payload.get("preferred_facilities")
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _aihr_live_route_evidence(current_query: str, target_query: str) -> dict:
    public_tools = tool_registry.mcp_tools_for_mode(operator_tools_enabled=False)
    return aihr_plan_route_evidence(
        current_query,
        target_query,
        available_tool_names=public_tools,
    )


def _missing_aihr_live_query_route_fields(route: Any) -> list[str]:
    if not isinstance(route, dict):
        return ["query_route"]
    missing: list[str] = []
    if route.get("schema") != QUERY_ROUTE_SCHEMA:
        missing.append("query_route.schema")
    if route.get("scenario") != "education_system_design":
        missing.append(f"query_route.scenario:{route.get('scenario')}")
    if route.get("tool") != PLAN_NCS_EDUCATION_PATH_TOOL:
        missing.append(f"query_route.tool:{route.get('tool')}")
    if route.get("available") is not True:
        missing.append(f"query_route.available:{route.get('available')}")
    if not route.get("route_fingerprint"):
        missing.append("query_route.route_fingerprint")
    if "guard_flags" not in route or not isinstance(route.get("guard_flags"), list):
        missing.append("query_route.guard_flags")
    expected_chain = route.get("expected_tool_chain")
    if not isinstance(expected_chain, list) or PLAN_NCS_EDUCATION_PATH_TOOL not in expected_chain:
        missing.append(f"query_route.expected_tool_chain.{PLAN_NCS_EDUCATION_PATH_TOOL}")
    if not isinstance(expected_chain, list) or "recommend_training_transition" not in expected_chain:
        missing.append("query_route.expected_tool_chain.recommend_training_transition")
    route_contract = route.get("route_contract")
    if not isinstance(route_contract, dict):
        missing.append("query_route.route_contract")
    else:
        if route_contract.get("schema") != QUERY_ROUTE_SCHEMA:
            missing.append("query_route.route_contract.schema")
        if route_contract.get("route_first") is not True:
            missing.append("query_route.route_contract.route_first")
        if route_contract.get("primary_tool") != route.get("tool"):
            missing.append("query_route.route_contract.primary_tool")
        if route_contract.get("route_fingerprint") != route.get("route_fingerprint"):
            missing.append("query_route.route_contract.route_fingerprint")
    return missing


def build_aihr_live_plan(db_path: Path, payload: dict) -> dict:
    current_query = _payload_text(payload, "current_query")
    target_query = _payload_text(payload, "target_query")
    if not current_query or not target_query:
        return {
            "ok": False,
            "error": {
                "code": "missing_required_query",
                "message": "current_query and target_query are required.",
            },
            "live_runner_schema": "aihr_live_plan_v1",
        }
    limit = _payload_int(payload, "limit", 3)
    route_evidence = _aihr_live_route_evidence(current_query, target_query)
    try:
        conn = connect_db_readonly(db_path)
    except sqlite3.Error as exc:
        return {
            "ok": False,
            "error": {
                "code": "database_unavailable",
                "message": "AI-HR live planner requires an existing readable NCS database.",
                "detail": str(exc),
            },
            "live_runner_schema": "aihr_live_plan_v1",
            "run_mode": "live_no_save",
        }
    try:
        transition = recommend_training_transition(
            conn,
            current_query=current_query,
            target_query=target_query,
            mode=_payload_text(payload, "mode", "all") or "all",
            preferred_max_hours=_payload_float(payload, "preferred_max_hours"),
            preferred_methods=_payload_methods(payload),
            preferred_facilities=_payload_facilities(payload),
            limit=limit,
            save=False,
        )
        plan = compact_ncs_education_plan_response(
            transition,
            plan_objective=_payload_text(payload, "plan_objective") or None,
            target_population=_payload_text(payload, "target_population") or None,
            scenario=_payload_text(payload, "scenario") or None,
            recommendation_limit=limit,
        )
        public_plan = public_demo_payload(plan)
        public_plan["live_runner_schema"] = "aihr_live_plan_v1"
        public_plan["run_mode"] = "live_no_save"
        public_plan["requested_input"] = {
            "current_query": current_query,
            "target_query": target_query,
            "limit": limit,
            "preferred_max_hours": _payload_float(payload, "preferred_max_hours"),
            "preferred_methods": _payload_methods(payload),
            "preferred_facilities": _payload_facilities(payload),
        }
        public_plan["query_route"] = route_evidence
        route_contract = (
            route_evidence.get("route_contract")
            if isinstance(route_evidence.get("route_contract"), dict)
            else {}
        )
        public_plan["route_contract_schema"] = route_contract.get("schema")
        public_plan["route_fingerprint"] = route_evidence.get("route_fingerprint")
        public_plan["route_guard_flags"] = [
            flag.get("code")
            for flag in route_evidence.get("guard_flags", [])
            if isinstance(flag, dict) and flag.get("code")
        ]
        missing_route_fields = _missing_aihr_live_query_route_fields(route_evidence)
        public_plan["missing_query_route_fields"] = missing_route_fields
        if missing_route_fields:
            public_plan = {
                "ok": False,
                "error": {
                    "code": "missing_query_route_contract",
                    "message": "AI-HR live planner route contract is incomplete.",
                    "missing_fields": missing_route_fields,
                },
                "live_runner_schema": "aihr_live_plan_v1",
                "run_mode": "live_no_save",
                "requested_input": {
                    "current_query": current_query,
                    "target_query": target_query,
                    "limit": limit,
                    "preferred_max_hours": _payload_float(payload, "preferred_max_hours"),
                    "preferred_methods": _payload_methods(payload),
                    "preferred_facilities": _payload_facilities(payload),
                },
                "query_route": route_evidence,
                "route_contract_schema": route_contract.get("schema"),
                "route_fingerprint": route_evidence.get("route_fingerprint"),
                "route_guard_flags": [
                    flag.get("code")
                    for flag in route_evidence.get("guard_flags", [])
                    if isinstance(flag, dict) and flag.get("code")
                ],
                "missing_query_route_fields": missing_route_fields,
            }
        return public_plan
    finally:
        conn.close()


def render_aihr_live_html() -> str:
    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI-HR Live Planner</title>
  <style>
    :root { --ink:#172033; --muted:#667085; --line:#d9e0ea; --bg:#f6f8fb; --panel:#fff; --accent:#0f766e; --warn:#b45309; --bad:#b91c1c; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Segoe UI, Arial, "Malgun Gothic", sans-serif; background:var(--bg); color:var(--ink); line-height:1.45; }
    main { max-width:1280px; margin:0 auto; padding:24px; }
    h1 { margin:0 0 6px; font-size:26px; }
    h2 { margin:22px 0 10px; font-size:18px; }
    .muted { color:var(--muted); }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; margin:14px 0; }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
    label { display:block; font-size:12px; font-weight:700; color:#445166; margin-bottom:4px; }
    input, select { width:100%; min-height:38px; border:1px solid #cbd5e1; border-radius:6px; padding:8px 10px; font:inherit; background:#fff; }
    button { min-height:38px; border:1px solid var(--accent); border-radius:6px; background:var(--accent); color:#fff; padding:8px 12px; font-weight:700; cursor:pointer; }
    button.secondary { background:#fff; color:var(--accent); }
    .toolbar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:12px; }
    .status { min-height:24px; margin-top:10px; font-weight:700; }
    .status.error { color:var(--bad); }
    .summary { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:10px; background:#fbfdff; }
    .metric span { display:block; color:var(--muted); font-size:12px; }
    .metric strong { font-size:20px; }
    .tags { display:flex; gap:6px; flex-wrap:wrap; }
    .tags span { border:1px solid var(--line); border-radius:999px; padding:5px 8px; background:#fff; font-size:12px; }
    table { width:100%; border-collapse:collapse; table-layout:fixed; background:#fff; }
    th,td { border:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; overflow-wrap:anywhere; font-size:13px; }
    th { background:#eef2f5; }
    .notice { border-left:4px solid var(--accent); padding:8px 10px; background:#eefaf8; color:#24443f; }
    pre { white-space:pre-wrap; background:#0f172a; color:#e2e8f0; padding:12px; border-radius:8px; overflow:auto; max-height:360px; }
    @media (max-width:760px) { main { padding:12px; } .grid,.summary { grid-template-columns:1fr; } table { display:block; overflow-x:auto; } th,td { min-width:150px; } }
  </style>
</head>
<body>
<main>
  <h1>AI-HR Live Planner</h1>
  <p class="muted">NCS 직무/과업/KSA와 훈련정보를 즉시 연결해 교육훈련체계 초안을 생성합니다. 2026 NCS 활용 가이드는 계획 검증 루브릭으로만 사용됩니다.</p>
  <section class="panel">
    <div class="grid">
      <div><label for="currentQuery">Current job or scope</label><input id="currentQuery" value="노무관리"></div>
      <div><label for="targetQuery">Target job or scope</label><input id="targetQuery" value="인사기획"></div>
      <div><label for="targetPopulation">Target population</label><input id="targetPopulation" value="인사담당자"></div>
      <div><label for="scenario">Scenario</label><input id="scenario" value="직무전환"></div>
      <div><label for="preferredMethods">Preferred methods, comma-separated</label><input id="preferredMethods" value="집체훈련"></div>
      <div><label for="preferredFacilities">Preferred facilities, comma-separated</label><input id="preferredFacilities" value=""></div>
      <div><label for="preferredMaxHours">Preferred max hours</label><input id="preferredMaxHours" value="24"></div>
      <div><label for="limit">Course limit</label><select id="limit"><option>3</option><option>5</option><option>7</option></select></div>
      <div><label for="mode">Mode</label><select id="mode"><option value="all">all</option><option value="upskilling">upskilling</option><option value="reskilling">reskilling</option></select></div>
    </div>
    <div class="toolbar">
      <button id="runButton" onclick="runPlanner()">Run Planner</button>
      <button class="secondary" onclick="seed('노무관리','인사기획','24')">노무관리 → 인사기획</button>
      <button class="secondary" onclick="seed('복무관리','인사기획','16')">복무관리 → 인사기획</button>
    </div>
    <div id="status" class="status muted"></div>
  </section>
  <section id="result" class="panel">
    <p class="muted">Run a scenario to see resolved NCS scope, KSA gaps, recommended courses, and a training-system matrix.</p>
  </section>
</main>
<script>
const q = (id) => document.getElementById(id);
const COURSE_INTAKE_REQUIREMENTS_SCHEMA = 'aihr_course_intake_requirements_v1';
const TRAINING_COURSE_INVENTORY_TEMPLATE_SCHEMA = 'aihr_training_course_inventory_template_v1';
const TRAINING_NECESSITY_REVIEW_SCHEMA = 'aihr_training_necessity_review_v1';
const ANNUAL_OPERATION_PLAN_SCHEMA = 'aihr_annual_operation_plan_seed_v1';
const esc = (text) => String(text ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
function join(values) {
  if (!values) return '-';
  if (Array.isArray(values)) return values.filter(Boolean).map(esc).join(', ') || '-';
  return esc(values);
}
function warningStatus(warning, fallback = 'clear') {
  if (!warning) return fallback;
  if (typeof warning === 'string') return warning;
  return warning.status || warning.severity || warning.level || fallback;
}
function warningCodes(warning) {
  if (!warning || typeof warning !== 'object') return '-';
  return join(warning.codes || warning.risk_codes || warning.code || warning.risk_code || warning.warning_code);
}
function seed(current, target, hours) {
  q('currentQuery').value = current;
  q('targetQuery').value = target;
  q('preferredMaxHours').value = hours;
}
function applyInitialPlannerQueryParams() {
  const search = new URLSearchParams(window.location.search);
  if (!search.toString()) return;
  const initialPlannerMappings = [
    ['currentQuery', ['current_query', 'currentQuery']],
    ['targetQuery', ['target_query', 'targetQuery']],
    ['targetPopulation', ['target_population', 'targetPopulation']],
    ['scenario', ['scenario']],
    ['preferredMethods', ['preferred_methods', 'preferredMethods']],
    ['preferredFacilities', ['preferred_facilities', 'preferredFacilities']],
    ['preferredMaxHours', ['preferred_max_hours', 'preferredMaxHours']],
    ['limit', ['limit']],
    ['mode', ['mode']]
  ];
  for (const [id, keys] of initialPlannerMappings) {
    for (const key of keys) {
      if (search.has(key)) {
        q(id).value = search.get(key) || '';
        break;
      }
    }
  }
}
function metric(label, value) {
  return `<div class="metric"><span>${esc(label)}</span><strong>${esc(value ?? '-')}</strong></div>`;
}
function renderGuideTrace(trace) {
  if (!trace || !trace.checks) return '<p class="muted">No guide trace returned.</p>';
  const workflow = trace.guide_workflow || {};
  const stages = Array.isArray(trace.guide_workflow_stages)
    ? trace.guide_workflow_stages
    : (Array.isArray(workflow.steps) ? workflow.steps : []);
  const checks = Array.isArray(trace.checks) ? trace.checks : [];
  const stageRows = stages.map(item => {
    const status = item.status || 'unknown';
    return `<tr>
      <td>${esc(item.code)}</td>
      <td>${esc(status)}</td>
      <td>${esc(item.title)}</td>
      <td>${esc(item.evidence)}</td>
    </tr>`;
  }).join('');
  const checkRows = checks.map(item => {
    const status = item.status || 'unknown';
    return `<tr>
      <td>${esc(item.code)}</td>
      <td>${esc(status)}</td>
      <td>${esc(item.label)}</td>
      <td>${esc(item.evidence)}</td>
    </tr>`;
  }).join('');
  return `<table><thead><tr><th>Guide Step</th><th>Status</th><th>Label</th><th>Evidence</th></tr></thead><tbody>${stageRows}${checkRows}</tbody></table>
  <pre>${esc(JSON.stringify({
    schema: trace.schema,
    rubric_source: trace.rubric_source,
    rubric_role: trace.rubric_role,
    non_source_data_policy: trace.non_source_data_policy,
    guide_workflow_stage_codes: trace.guide_workflow_stage_codes,
    status_counts: trace.status_counts,
    matrix_reconstruction_fields: trace.matrix_reconstruction_fields
  }, null, 2))}</pre>`;
}
function renderRouteEvidence(route) {
  if (!route) return '<p class="muted">No route evidence returned.</p>';
  const guardFlags = Array.isArray(route.guard_flags) ? route.guard_flags.map(flag => flag.code || flag).filter(Boolean) : [];
  return `<table><tbody>
    <tr><th>Schema</th><td>${esc(route.schema)}</td></tr>
    <tr><th>Scenario</th><td>${esc(route.scenario)}</td></tr>
    <tr><th>Primary Tool</th><td><code>${esc(route.tool)}</code></td></tr>
    <tr><th>Expected Tool Chain</th><td>${join(route.expected_tool_chain)}</td></tr>
    <tr><th>Confidence</th><td>${esc(route.confidence)}</td></tr>
    <tr><th>Available</th><td>${esc(route.available)}</td></tr>
    <tr><th>Guard Flags</th><td>${join(guardFlags)}</td></tr>
    <tr><th>Route Fingerprint</th><td><code>${esc(route.route_fingerprint)}</code></td></tr>
  </tbody></table>
  <pre>${esc(JSON.stringify({
    query: route.query,
    required_params: route.required_params,
    missing_params: route.missing_params,
    route_contract: route.route_contract
  }, null, 2))}</pre>`;
}
function renderRecommendedPath(path) {
  const stages = Array.isArray(path) ? path : [];
  if (!stages.length) return '<p class="muted">No recommended_path stages returned.</p>';
  return `<table><thead><tr><th>Stage</th><th>Role</th><th>Guide</th><th>Actions</th><th>Courses / Output</th></tr></thead><tbody>${stages.map(stage => {
    const actions = Array.isArray(stage.actions) ? stage.actions : [];
    const courses = Array.isArray(stage.courses) ? stage.courses : [];
    const outputs = Array.isArray(stage.outputs) ? stage.outputs : [];
    const constraints = stage.constraints ? JSON.stringify(stage.constraints) : '';
    const guideEvidence = stage.guide_stage_evidence || {};
    return `<tr>
      <td>${esc(stage.stage)}</td>
      <td><strong>${esc(stage.role)}</strong><br><span class="muted">${esc(stage.title || '')}</span></td>
      <td><strong>${esc(stage.guide_stage || '')}</strong><br><span class="muted">${esc(stage.guide_stage_status || '')}</span><br><span class="muted">${esc(guideEvidence.evidence || '')}</span></td>
      <td>${actions.map(action => `<div>${esc(action)}</div>`).join('') || esc(stage.selection_rule || '')}</td>
      <td>${courses.length ? courses.map(course => `<div><strong>${esc(course.course_name)}</strong> <span class="muted">${esc(course.tier || '')}</span></div>`).join('') : join(outputs)}<br><span class="muted">${esc(constraints)}</span></td>
    </tr>`;
  }).join('')}</tbody></table>`;
}
function renderMatrix(rows) {
  if (!rows || !rows.length) return '<p class="muted">No training-system matrix rows returned.</p>';
  return `<table><thead><tr><th>Rank</th><th>Job / Scope</th><th>Course</th><th>Required / Optional</th><th>Education Type</th><th>Evidence</th><th>Course Scope Fit</th><th>Evidence Chain</th><th>KSA Basis</th><th>Level / Delivery</th><th>Warnings</th><th>Decision State</th><th>Review</th></tr></thead><tbody>${rows.map(row => {
    const need = row.need_classification || {};
    const direct = row.evidence_directness || {};
    const scopeFit = row.course_scope_fit || row.course_link?.course_scope_fit || {};
    const basis = row.task_ksa_basis || {};
    const fit = row.course_fit || {};
    const jobScope = row.job_scope || {};
    const levelBand = row.target_level_band || {};
    const educationType = row.education_type || {};
    const deliveryOperation = row.delivery_operation || {};
    const facilityFit = row.facility_constraint_fit || row.delivery_operation?.facility_constraint_fit || {};
    const methodFit = deliveryOperation.method_constraint_fit || {};
    const timeFit = deliveryOperation.time_constraint_fit || {};
    const constraintFit = deliveryOperation.constraint_fit || {};
    const humanReview = row.human_review || {};
    const specificity = row.specificity_warning || {};
    const duplicate = row.duplicate_or_generic_warning || {};
    const mappingStrength = row.mapping_strength_warning || {};
    const decisionState = row.decision_state || {};
    const evidenceChain = row.evidence_chain || {};
    const evidenceChainCompleteness = evidenceChain.completeness || {};
    const evidenceChainLinks = Array.isArray(evidenceChain.links) ? evidenceChain.links : [];
    const evidenceChainText = evidenceChainLinks.slice(0, 5).map(link => {
      const value = Array.isArray(link.value) ? link.value.slice(0, 4).join(', ') : (link.value || '');
      return `${esc(link.stage || '')}: ${esc(value)}`;
    }).join('<br>');
    return `<tr>
      <td>${esc(row.rank)}</td>
      <td>${esc(jobScope.transition || row.current_scope || '')}<br><span class="muted">${esc(row.planner_grouping?.job_scope || '')}</span></td>
      <td><strong>${esc(row.course_name)}</strong><br><span class="muted">${esc(row.training_course_id || '')}</span></td>
      <td>${esc(need.label || need.code || 'unknown')}<br><span class="muted">${esc(need.rationale || need.reason || '')}</span></td>
      <td>${esc(educationType.label || educationType.code || 'unknown')}<br><span class="muted">${esc(educationType.rationale || '')}</span></td>
      <td>${esc(direct.label || direct.code || 'unknown')}<br><span class="muted">${esc(direct.reason || '')}</span></td>
      <td>${esc(scopeFit.label || scopeFit.relation || 'unknown')}<br><span class="muted">${esc(scopeFit.relation || 'unknown')} / ${esc(scopeFit.alignment || 'unknown')}</span><br><span class="muted">${join(scopeFit.direct_unit_codes)}</span></td>
      <td><strong>evidence_chain:</strong> ${esc(evidenceChainCompleteness.status || 'partial')}<br><span class="muted">${evidenceChainText}</span></td>
      <td><strong>${join(basis.basis_types)}</strong><br>${join(basis.gap_ksa || basis.training_goal_ksa || basis.target_scope_ksa)}<br><span class="muted">Elements: ${join(basis.covered_elements)}</span></td>
      <td>${esc(levelBand.label || 'unknown')}<br>Level ${esc(fit.level || 'unknown')}<br>${esc(fit.hours ?? 'unknown')}h<br>${join(fit.methods)}<br>${join(fit.facilities)}<br><span class="muted">Method fit: ${esc(methodFit.status || 'not_requested')}</span><br><span class="muted">Time fit: ${esc(timeFit.status || 'not_requested')}</span><br><span class="muted">Facility fit: ${esc(facilityFit.status || 'not_requested')}</span><br><span class="muted">Constraint fit: ${esc(constraintFit.status || 'not_requested')}</span></td>
      <td><strong>Specificity:</strong> ${esc(warningStatus(specificity))}<br><span class="muted">${warningCodes(specificity)}</span><br><strong>Duplicate/generic:</strong> ${esc(warningStatus(duplicate))}<br><span class="muted">${warningCodes(duplicate)}</span><br><strong>Mapping strength:</strong> ${esc(warningStatus(mappingStrength))}<br><span class="muted">${warningCodes(mappingStrength)}</span></td>
      <td><strong>decision_state:</strong> ${esc(decisionState.status || 'pending_human_decision')}<br><span class="muted">suggestion: ${esc(decisionState.system_suggestion || need.code || 'unknown')}</span><br><span class="muted">approval_claim: ${esc(decisionState.approval_claim)}</span></td>
      <td>${esc(humanReview.severity || 'ready')}<br>${join(row.review_flags)}<br><span class="muted">${esc(humanReview.prompt || '')}</span></td>
    </tr>`;
  }).join('')}</tbody></table>`;
}
function renderAnnualOperationPlan(plan) {
  if (!plan || !plan.rows) return '<p class="muted">No annual_operation_plan seed returned.</p>';
  const summary = plan.summary || {};
  const gate = plan.review_gate || {};
  const rows = Array.isArray(plan.rows) ? plan.rows : [];
  return `<div class="notice"><strong>annual_operation_plan:</strong> ${esc(plan.schema || '')} / ${esc(plan.status || '')}<br><span class="muted">${esc(plan.purpose || '')}</span><br><span class="muted">rows=${esc(summary.row_count)}, hours=${esc(summary.estimated_total_hours)}, pending=${esc(summary.pending_human_decision_rows)}, approval_claim=${esc(gate.approval_claim)}</span></div>
  <table><thead><tr><th>#</th><th>Window / Phase</th><th>Course</th><th>Need</th><th>Delivery</th><th>Constraint</th><th>Decision</th><th>Review</th></tr></thead><tbody>${rows.map(row => `
    <tr>
      <td>${esc(row.sequence)}</td>
      <td>${esc(row.recommended_window)}<br><span class="muted">${esc(row.phase)}</span></td>
      <td><strong>${esc(row.course_name)}</strong><br><span class="muted">${esc(row.training_course_id || '')}</span></td>
      <td>${esc(row.need_classification)}<br><span class="muted">${esc(row.system_suggestion || '')}</span></td>
      <td>${esc(row.hours ?? 'unknown')}h<br>${join(row.methods)}<br>${join(row.facilities)}</td>
      <td>${esc(row.constraint_status || 'unknown')}<br><span class="muted">method=${esc(row.method_status || '')}, time=${esc(row.time_status || '')}, facility=${esc(row.facility_status || '')}</span></td>
      <td>${esc(row.decision_status || '')}<br><span class="muted">chain=${esc(row.evidence_chain_status || '')}</span></td>
      <td>${esc(row.human_review_severity || '')}<br>${join(row.review_flags)}<br><span class="muted">${esc(row.scheduling_rationale || '')}</span></td>
    </tr>`).join('')}</tbody></table>`;
}
function renderCourseIntakeRequirements(intake) {
  if (!intake || !intake.required_fields) return '<p class="muted">No course_intake_requirements returned.</p>';
  const required = Array.isArray(intake.required_fields) ? intake.required_fields : [];
  const policy = intake.mapping_policy || {};
  const prefill = intake.prefill_from_recommendations || {};
  const gate = intake.review_gate || {};
  return `<div class="notice"><strong>course_intake_requirements:</strong> ${esc(intake.schema || '')} / ${esc(intake.status || '')}<br><span class="muted">${esc(intake.purpose || '')}</span><br><span class="muted">courses=${esc(prefill.course_count)}, title_only_allowed=${esc(policy.title_only_mapping_allowed)}, approval_claim=${esc(gate.approval_claim)}</span></div>
  <table><thead><tr><th>Required Field</th><th>Why Collected</th><th>Maps To</th></tr></thead><tbody>${required.map(item => `
    <tr>
      <td>${esc(item.field || '')}</td>
      <td>${esc(item.purpose || '')}</td>
      <td>${join(item.maps_to)}</td>
    </tr>`).join('')}</tbody></table>`;
}
function renderTrainingCourseInventoryTemplate(template) {
  if (!template || !template.columns) return '<p class="muted">No training_course_inventory_template returned.</p>';
  const columns = Array.isArray(template.columns) ? template.columns : [];
  const rows = Array.isArray(template.prefill_rows) ? template.prefill_rows : [];
  const gate = template.review_gate || {};
  return `<div class="notice"><strong>training_course_inventory_template:</strong> ${esc(template.schema || '')} / ${esc(template.status || '')}<br><span class="muted">${esc(template.purpose || '')}</span><br><span class="muted">columns=${esc(columns.length)}, prefill=${esc(rows.length)}, approval_claim=${esc(gate.approval_claim)}</span></div>
  <table><thead><tr><th>Column</th><th>Required</th><th>Why Collected</th><th>Maps To</th></tr></thead><tbody>${columns.map(item => `
    <tr>
      <td>${esc(item.column || '')}</td>
      <td>${esc(item.required)}</td>
      <td>${esc(item.purpose || '')}</td>
      <td>${join(item.maps_to)}</td>
    </tr>`).join('')}</tbody></table>
  <table><thead><tr><th>Source</th><th>Course</th><th>Goal</th><th>KSA</th><th>Hours / Method</th><th>Risk</th><th>Review</th></tr></thead><tbody>${rows.map(row => `
    <tr>
      <td>${esc(row.source_type || '')}</td>
      <td><strong>${esc(row.course_name || '')}</strong><br><span class="muted">${esc(row.training_course_id || '')}</span></td>
      <td>${esc(row.course_goal || '')}</td>
      <td>${join(row.ksa_evidence)}</td>
      <td>${esc(row.hours ?? 'unknown')}h<br>${join(row.methods)}</td>
      <td>${esc(row.duplicate_or_generic_risk || '')}</td>
      <td>${esc(row.review_state || '')}</td>
    </tr>`).join('')}</tbody></table>`;
}
function renderTrainingNecessityReview(review) {
  if (!review || !review.rows) return '<p class="muted">No training_necessity_review returned.</p>';
  const summary = review.summary || {};
  const rows = Array.isArray(review.rows) ? review.rows : [];
  const gate = review.review_gate || {};
  return `<div class="notice"><strong>training_necessity_review:</strong> ${esc(review.schema || '')} / ${esc(review.status || '')}<br><span class="muted">${esc(review.purpose || '')}</span><br><span class="muted">rows=${esc(summary.row_count)}, review_required=${esc(summary.review_required_rows)}, approval_blocked=${esc(summary.approval_blocked_rows)}, approval_claim=${esc(gate.approval_claim)}</span></div>
  <table><thead><tr><th>#</th><th>Course</th><th>Job Linkage</th><th>Level</th><th>Req/Opt</th><th>Risk</th><th>Delivery</th><th>Performance</th><th>Decision</th></tr></thead><tbody>${rows.map(row => {
    const job = row.job_linkage || {};
    const level = row.level_fit || {};
    const need = row.required_optional_review || {};
    const risk = row.duplicate_or_generic_review || {};
    const delivery = row.delivery_feasibility || {};
    const contribution = row.performance_contribution || {};
    const decision = row.decision_state || {};
    return `<tr>
      <td>${esc(row.sequence)}</td>
      <td><strong>${esc(row.course_name || '')}</strong><br><span class="muted">${esc(row.training_course_id || '')}</span></td>
      <td>${esc(job.status || '')}<br><span class="muted">${esc(job.course_scope_relation || '')} / ${esc(job.evidence_directness || '')}</span></td>
      <td>${esc(level.status || '')}<br><span class="muted">level=${esc(level.course_level ?? 'unknown')}</span></td>
      <td>${esc(need.code || '')}<br><span class="muted">approval_claim=${esc(need.approval_claim)}</span></td>
      <td>${esc(risk.status || '')}<br><span class="muted">${join(risk.codes)}</span></td>
      <td>${esc(delivery.status || '')}<br><span class="muted">${esc(delivery.constraint_status || '')}</span></td>
      <td>${esc(contribution.status || '')}<br><span class="muted">chain=${esc(contribution.evidence_chain_status || '')}</span></td>
      <td>${esc(decision.status || '')}<br><span class="muted">${esc(row.recommended_review_action || '')}</span></td>
    </tr>`;
  }).join('')}</tbody></table>`;
}
function renderPlan(data) {
  if (!data.ok) {
    q('result').innerHTML = `<h2>Input Needed</h2><pre>${esc(JSON.stringify(data, null, 2))}</pre>`;
    return;
  }
  const summary = data.training_system_summary || {};
  const current = data.current_scope || {};
  const target = data.target_scope || {};
  const transition = data.transition_assessment || {};
  const gaps = data.priority_gaps || [];
  const inputQuality = data.input_quality || {};
  q('result').innerHTML = `
    <div class="notice">${esc(data.public_demo_notice || data.disclaimer || '')}</div>
    <h2>${esc(data.plan_objective || 'NCS education plan')}</h2>
    <div class="summary">
      ${metric('Current scope', current.resolved_as || data.requested_input?.current_query)}
      ${metric('Target scope', target.resolved_as || data.requested_input?.target_query)}
      ${metric('Courses', summary.course_count)}
      ${metric('Scenario', data.scenario?.title || data.scenario?.selected)}
    </div>
    <h2>Route Evidence</h2>
    ${renderRouteEvidence(data.query_route)}
    <h2>Transition Summary</h2>
    <p>${esc(transition.summary || '')}</p>
    <h2>Priority Gap KSA</h2>
    <div class="tags">${gaps.length ? gaps.map(gap => `<span>${esc(gap)}</span>`).join('') : '<span>none</span>'}</div>
    <h2>Recommended Path</h2>
    ${renderRecommendedPath(data.recommended_path)}
    <h2>2026 Guide Trace</h2>
    ${renderGuideTrace(data.training_system_guide_trace)}
    <h2>Course Intake Requirements</h2>
    ${renderCourseIntakeRequirements(data.course_intake_requirements)}
    <h2>Training Course Inventory Template</h2>
    ${renderTrainingCourseInventoryTemplate(data.training_course_inventory_template)}
    <h2>Training Necessity Review</h2>
    ${renderTrainingNecessityReview(data.training_necessity_review)}
    <h2>Annual Operation Plan Seed</h2>
    ${renderAnnualOperationPlan(data.annual_operation_plan)}
    <h2>Training-System Matrix</h2>
    ${renderMatrix(data.training_system_matrix || [])}
    <h2>Input Quality</h2>
    <pre>${esc(JSON.stringify(inputQuality, null, 2))}</pre>
  `;
}
async function runPlanner() {
  q('status').className = 'status muted';
  q('status').textContent = 'Running planner...';
  q('runButton').disabled = true;
  try {
    const payload = {
      current_query: q('currentQuery').value,
      target_query: q('targetQuery').value,
      target_population: q('targetPopulation').value,
      scenario: q('scenario').value,
      preferred_methods: q('preferredMethods').value,
      preferred_facilities: q('preferredFacilities').value,
      preferred_max_hours: q('preferredMaxHours').value,
      limit: q('limit').value,
      mode: q('mode').value
    };
    const res = await fetch('/api/aihr-plan', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    const data = await res.json();
    if (!res.ok) throw new Error(JSON.stringify(data));
    renderPlan(data);
    q('status').textContent = `Rendered ${data.training_system_matrix?.length || 0} matrix rows.`;
  } catch (err) {
    q('status').className = 'status error';
    q('status').textContent = err.message;
  } finally {
    q('runButton').disabled = false;
  }
}
applyInitialPlannerQueryParams();
</script>
</body>
</html>
"""


def _aihr_training_builder_workflow_html() -> str:
    return """
  <section class="panel">
    <h2>2026 Guide Workflow</h2>
    <table>
      <thead><tr><th>Step</th><th>What the program resolves</th><th>Runtime evidence</th></tr></thead>
      <tbody>
        <tr><td>Job Scope</td><td>Current and target NCS scope</td><td>query_route, current_scope, target_scope</td></tr>
        <tr><td>Task and KSA</td><td>Performance criteria, KSA gaps, transferable KSA</td><td>priority_gaps, task_ksa_basis</td></tr>
        <tr><td>Course Map</td><td>NCS training-course links to tasks and KSA</td><td>recommended_path, training_system_matrix</td></tr>
        <tr><td>Course Intake Requirements</td><td>C1-1 intake fields for internal and external course investigation</td><td>course_intake_requirements, aihr_course_intake_requirements_v1</td></tr>
        <tr><td>Training Course Inventory Template</td><td>C1-1 inventory columns and prefilled course rows for investigated courses</td><td>training_course_inventory_template, aihr_training_course_inventory_template_v1</td></tr>
        <tr><td>Training Necessity Review</td><td>C1-2 job linkage, level fit, required/optional basis, duplicate risk, delivery feasibility, and performance contribution review</td><td>training_necessity_review, aihr_training_necessity_review_v1</td></tr>
        <tr><td>Required / Optional</td><td>Required, supporting, optional, adjacent-reference classification</td><td>required_optional_basis.rationale</td></tr>
        <tr><td>Level / Delivery</td><td>Level, hours, method, facility fit</td><td>course_fit, delivery_operation, facility_constraint_fit</td></tr>
        <tr><td>Warnings</td><td>Specificity, duplicate/generic, and mapping-strength course risk</td><td>specificity_warning, duplicate_or_generic_warning, mapping_strength_warning</td></tr>
        <tr><td>Evidence Chain</td><td>NCS guide sequence from job scope through task/KSA to course</td><td>evidence_chain, aihr_course_evidence_chain_v1</td></tr>
        <tr><td>Decision State</td><td>System suggestion separated from human confirmation</td><td>decision_state, approval_claim=false</td></tr>
        <tr><td>Annual Operation Plan</td><td>C2-2 scheduling seed that preserves delivery constraints and pending human decisions</td><td>annual_operation_plan, aihr_annual_operation_plan_seed_v1</td></tr>
        <tr><td>Human Review</td><td>Rows that need operator confirmation</td><td>human_review, review_flags</td></tr>
      </tbody>
    </table>
  </section>"""


def render_aihr_training_system_builder_html() -> str:
    html = render_aihr_live_html()
    html = html.replace("<title>AI-HR Live Planner</title>", "<title>AI-HR Training System Builder</title>")
    html = html.replace("<h1>AI-HR Live Planner</h1>", "<h1>AI-HR Training System Builder</h1>")
    return html.replace("  <section class=\"panel\">", _aihr_training_builder_workflow_html() + "\n  <section class=\"panel\">", 1)


def render_aihr_readiness_html(
    payload: dict,
    source_path: Path,
    *,
    triage_payload: dict | None = None,
    triage_path: Path | None = None,
) -> str:
    payload = public_aihr_dashboard_payload(payload)
    triage_payload = public_aihr_dashboard_payload(
        triage_payload if isinstance(triage_payload, dict) else {}
    )
    blockers = payload.get("blockers") or []
    warnings = payload.get("warnings") or []
    next_actions = payload.get("next_actions") or []
    mcp_checks = ((payload.get("checks") or {}).get("mcp_contract") or [])
    demo_contract = payload.get("demo_contract") or {}
    dashboard_contract = payload.get("dashboard_surface_contract") or {}
    dashboard_artifact = dashboard_contract.get("artifact") if isinstance(dashboard_contract.get("artifact"), dict) else {}
    review_chain_summary = (
        dashboard_artifact.get("review_chain_safety_summary")
        if isinstance(dashboard_artifact.get("review_chain_safety_summary"), dict)
        else {}
    )
    live_summaries = dashboard_artifact.get("live_plan_summaries") if isinstance(dashboard_artifact.get("live_plan_summaries"), list) else []
    static_artifacts = dashboard_artifact.get("static_artifacts") if isinstance(dashboard_artifact.get("static_artifacts"), list) else []
    queue_summary = dashboard_artifact.get("queue_status_summary") if isinstance(dashboard_artifact.get("queue_status_summary"), dict) else {}
    json_artifacts = demo_contract.get("json_artifacts") or []
    html_artifact = demo_contract.get("html_artifact") or {}
    triage_summary = triage_payload.get("summary") if isinstance(triage_payload.get("summary"), dict) else {}
    triage_snapshot = (
        triage_summary.get("transition_status_snapshot")
        if isinstance(triage_summary.get("transition_status_snapshot"), dict)
        else {}
    )
    triage_source_paths = (
        triage_summary.get("source_paths")
        if isinstance(triage_summary.get("source_paths"), dict)
        else {}
    )
    triage_issue_counts = (
        triage_summary.get("review_issue_type_counts")
        if isinstance(triage_summary.get("review_issue_type_counts"), dict)
        else {}
    )
    release_decision = (
        payload.get("release_decision")
        if isinstance(payload.get("release_decision"), dict)
        else {}
    )
    release_contract_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(key)}</td>"
        f"<td>{_html_escape(value)}</td>"
        "</tr>"
        for key, value in {
            "schema": payload.get("schema"),
            "ok": payload.get("ok"),
            "ok_meaning": payload.get("ok_meaning"),
            "release_decision_status": release_decision.get("status"),
            "release_ready": release_decision.get("release_ready", payload.get("release_ready")),
            "approval_claim": release_decision.get("approval_claim", payload.get("approval_claim")),
            "human_decision_required_for_release_claim": release_decision.get(
                "human_decision_required_for_release_claim"
            ),
            "blocked_by": release_decision.get("blocked_by"),
            "blocked_by_display_labels": release_decision.get("blocked_by_display_labels"),
        }.items()
    )
    blocker_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(item.get('category'))}</td>"
        f"<td>{_html_escape(item.get('display_label') or blocker_display_label(item.get('name')))}"
        f"<br><span class=\"muted\">{_html_escape(item.get('name'))}</span></td>"
        f"<td>{_html_escape(item.get('display_message') or blocker_display_message(item.get('name'), item.get('message')))}</td>"
        f"<td>{_html_escape(item.get('value'))}</td>"
        f"<td>{_html_escape(item.get('threshold'))}</td>"
        "</tr>"
        for item in blockers
    )
    warning_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(item.get('name'))}</td>"
        f"<td>{_html_escape(item.get('message'))}</td>"
        f"<td>{_html_escape(item.get('value'))}</td>"
        f"<td>{_html_escape(item.get('threshold'))}</td>"
        "</tr>"
        for item in warnings
    )
    next_action_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(item.get('owner'))}</td>"
        f"<td>{_html_escape(item.get('blocker_display_label') or blocker_display_label(item.get('blocker')))}"
        f"<br><span class=\"muted\">{_html_escape(item.get('blocker'))}</span></td>"
        f"<td>{_html_escape(item.get('action'))}</td>"
        f"<td><code>{_html_escape(item.get('command'))}</code></td>"
        "</tr>"
        for item in next_actions
    )
    demo_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(item.get('path'))}</td>"
        f"<td>{_html_escape(item.get('ok'))}</td>"
        f"<td>{_html_escape(item.get('view'))}</td>"
        f"<td>{_html_escape(item.get('matrix_rows'))}</td>"
        "</tr>"
        for item in json_artifacts
    )
    if html_artifact:
        demo_rows += (
            "<tr>"
            f"<td>{_html_escape(html_artifact.get('path'))}</td>"
            f"<td>{_html_escape(html_artifact.get('ok'))}</td>"
            "<td>html</td>"
            f"<td>{_html_escape(html_artifact.get('length'))}</td>"
            "</tr>"
        )
    dashboard_rows = "".join(
        (
        "<tr>"
        f"<td>{_html_escape(item.get('name'))}</td>"
        f"<td>{_html_escape(item.get('ok'))}</td>"
        f"<td>{_html_escape(item.get('matrix_rows'))}</td>"
        f"<td>{_html_escape((item.get('training_necessity_review_summary') or {}).get('row_count'))}</td>"
        f"<td>{_html_escape((item.get('training_necessity_review_summary') or {}).get('review_required_rows'))}</td>"
        f"<td>{_html_escape((item.get('training_necessity_review_summary') or {}).get('approval_blocked_rows'))}</td>"
        f"<td>{_html_escape((item.get('training_necessity_review_summary') or {}).get('approval_claim_safe'))}</td>"
        f"<td>{_html_escape((item.get('annual_operation_plan_summary') or {}).get('row_count'))}</td>"
        f"<td>{_html_escape((item.get('annual_operation_plan_summary') or {}).get('estimated_total_hours'))}</td>"
        f"<td>{_html_escape((item.get('annual_operation_plan_summary') or {}).get('pending_human_decision_rows'))}</td>"
        f"<td>{_html_escape((item.get('annual_operation_plan_summary') or {}).get('approval_claim_safe'))}</td>"
        f"<td>{_html_escape(item.get('guide_trace_schema'))}</td>"
        f"<td>{_html_escape(item.get('missing_matrix_fields'))}</td>"
        f"<td>{_html_escape(item.get('missing_plan_fields'))}</td>"
        f"<td>{_html_escape(item.get('missing_guide_trace_fields'))}</td>"
        f"<td>{_html_escape(item.get('missing_query_route_fields'))}</td>"
        f"<td>{_html_escape(item.get('sensitive_markers'))}</td>"
        "</tr>"
        )
        for item in live_summaries
        if isinstance(item, dict)
    )
    def static_artifact_checkpoint_summary(item: dict) -> tuple[object, object, str]:
        checkpoint = item.get("checkpoint") if isinstance(item.get("checkpoint"), dict) else {}
        if not checkpoint:
            return "", "", ""
        name = str(item.get("name") or "")
        if name == "ncs006_element_api_checkpoint_json":
            summary = (
                f"matched={checkpoint.get('matched')}/{checkpoint.get('total')}; "
                f"remaining={checkpoint.get('not_collected')}; "
                f"active={checkpoint.get('active_batch_monitor_status')}"
            )
        elif name == "human_review_safe_ops_checkpoint_json":
            summary = (
                f"pending={checkpoint.get('sqf_pending_decision_count')}; "
                f"provenance_gap={checkpoint.get('provenance_gap_present')}; "
                f"writes={checkpoint.get('sqf_planned_db_writes')}"
            )
        elif name == "sqf_db_readiness_checkpoint_json":
            summary = (
                f"status={checkpoint.get('status')}; "
                f"candidates={checkpoint.get('sqf_ncs_candidate_count')}; "
                f"scoring={checkpoint.get('used_for_scoring')}"
            )
        elif name == "overnight_ncs_sqf_work_checkpoint_json":
            summary = (
                f"ncs006={checkpoint.get('ncs006_matched')}/{checkpoint.get('ncs006_total')}; "
                f"sqf={checkpoint.get('sqf_status')}; "
                f"writes={checkpoint.get('human_review_planned_db_writes')}"
            )
        else:
            summary = str(checkpoint.get("status") or checkpoint.get("ok") or "")
        return checkpoint.get("contract_ok"), checkpoint.get("schema"), summary

    static_artifact_rows = ""
    for item in static_artifacts:
        if not isinstance(item, dict):
            continue
        checkpoint_ok, checkpoint_schema, checkpoint_summary = static_artifact_checkpoint_summary(item)
        static_artifact_rows += (
            "<tr>"
            f"<td>{_html_escape(item.get('name'))}</td>"
            f"<td>{_html_escape(item.get('exists'))}</td>"
            f"<td>{_html_escape(item.get('non_empty'))}</td>"
            f"<td>{_html_escape(item.get('size_bytes'))}</td>"
            f"<td>{_html_escape(item.get('path'))}</td>"
            f"<td>{_html_escape(checkpoint_ok)}</td>"
            f"<td>{_html_escape(checkpoint_schema)}</td>"
            f"<td>{_html_escape(checkpoint_summary)}</td>"
            "</tr>"
        )
    review_chain_rows = ""
    if review_chain_summary:
        review_chain_rows = (
            "<tr>"
            f"<td>{_html_escape(review_chain_summary.get('contract_ok'))}</td>"
            f"<td>{_html_escape(review_chain_summary.get('schema'))}</td>"
            f"<td>{_html_escape(review_chain_summary.get('source_payload_exposed'))}</td>"
            f"<td>{_html_escape(review_chain_summary.get('learning_module_visible_items'))}</td>"
            f"<td>{_html_escape(review_chain_summary.get('ncs_report_visible_items'))}</td>"
            f"<td>{_html_escape(review_chain_summary.get('ocr_context_card_count'))}</td>"
            f"<td>{_html_escape(review_chain_summary.get('blocked_automation_actions'))}</td>"
            f"<td>{_html_escape(review_chain_summary.get('issues'))}</td>"
            "</tr>"
        )
    mcp_check_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(item.get('name'))}</td>"
        f"<td>{_html_escape(item.get('ok'))}</td>"
        f"<td>{_html_escape(item.get('detail'))}</td>"
        "</tr>"
        for item in mcp_checks
    )
    triage_issue_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(key)}</td>"
        f"<td>{_html_escape(value)}</td>"
        "</tr>"
        for key, value in sorted(triage_issue_counts.items())
    )
    triage_source_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(key)}</td>"
        f"<td>{_html_escape(value)}</td>"
        "</tr>"
        for key, value in sorted(triage_source_paths.items())
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI-HR 준비도</title>
  <style>
    :root {{ --ink:#172033; --muted:#667085; --line:#d9e0ea; --bg:#f6f8fb; --panel:#fff; --ok:#047857; --warn:#b45309; --bad:#b91c1c; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Segoe UI, Arial, "Malgun Gothic", sans-serif; background:var(--bg); color:var(--ink); }}
    main {{ max-width:1180px; margin:0 auto; padding:24px; }}
    h1 {{ margin:0 0 6px; font-size:26px; }}
    h2 {{ margin:22px 0 8px; font-size:18px; }}
    .muted {{ color:var(--muted); }}
    .summary {{ display:grid; grid-template-columns:repeat(6, minmax(0,1fr)); gap:10px; margin-top:16px; }}
    .metric {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }}
    .metric span {{ display:block; color:var(--muted); font-size:12px; }}
    .metric strong {{ display:block; margin-top:4px; font-size:22px; }}
    .ok {{ color:var(--ok); }} .warn {{ color:var(--warn); }} .bad {{ color:var(--bad); }}
    table {{ width:100%; border-collapse:collapse; background:var(--panel); table-layout:fixed; }}
    th, td {{ border:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; overflow-wrap:anywhere; font-size:13px; }}
    th {{ background:#eef2f5; }}
    @media (max-width:760px) {{ main {{ padding:12px; }} .summary {{ grid-template-columns:1fr; }} table {{ display:block; overflow-x:auto; }} th, td {{ min-width:150px; }} }}
  </style>
</head>
<body>
<main>
  <p class="muted">{_html_escape(source_path.name)}</p>
  <h1>AI-HR 준비도</h1>
  <p class="muted">실행 데모 계약, MCP 도구 경계, 품질 게이트, human review 병목을 한 화면에서 확인합니다.</p>
  <section class="summary">
    <div class="metric"><span>release_ready</span><strong class="{ 'ok' if payload.get('release_ready') else 'bad' }">{_html_escape(payload.get('release_ready'))}</strong></div>
    <div class="metric"><span>engineering_hygiene_ok</span><strong class="{ 'ok' if payload.get('engineering_hygiene_ok') else 'bad' }">{_html_escape(payload.get('engineering_hygiene_ok'))}</strong></div>
    <div class="metric"><span>blockers</span><strong class="{ 'ok' if not blockers else 'warn' }">{len(blockers)}</strong></div>
    <div class="metric"><span>demo_contract</span><strong class="{ 'ok' if demo_contract.get('ok') else 'bad' }">{_html_escape(demo_contract.get('ok'))}</strong></div>
    <div class="metric"><span>dashboard_surface</span><strong class="{ 'ok' if dashboard_contract.get('ok') else 'bad' }">{_html_escape(dashboard_contract.get('ok'))}</strong></div>
    <div class="metric"><span>review_chain_safety</span><strong class="{ 'ok' if review_chain_summary.get('contract_ok') else 'bad' }">{_html_escape(review_chain_summary.get('contract_ok') if review_chain_summary else 'not checked')}</strong></div>
    <div class="metric"><span>live_scenarios</span><strong>{len(live_summaries)}</strong></div>
    <div class="metric"><span>queue_blocked</span><strong class="{ 'ok' if not queue_summary.get('blocked_count') else 'bad' }">{_html_escape(queue_summary.get('blocked_count'))}</strong></div>
    <div class="metric"><span>static_artifacts</span><strong>{len(static_artifacts)}</strong></div>
    <div class="metric"><span>transition_attention</span><strong>{_html_escape(triage_summary.get('transition_attention_count') or 'not checked')}</strong></div>
    <div class="metric"><span>transition_seedpack_items</span><strong>{_html_escape(triage_summary.get('transition_seedpack_item_count') or 'not checked')}</strong></div>
    <div class="metric"><span>trusted_in_seedpack</span><strong>{_html_escape(triage_snapshot.get('trusted_review_status_count') if triage_snapshot else 'not checked')}</strong></div>
  </section>
  <h2>Release Decision Contract</h2>
  <table><thead><tr><th>Field</th><th>Value</th></tr></thead><tbody>{release_contract_rows}</tbody></table>
  <h2>AI-HR Review Triage</h2>
  <p class="muted">triage_artifact={_html_escape(_public_aihr_path_text(triage_path) if triage_path else 'not checked')} | seedpack_id={_html_escape(triage_summary.get('transition_seedpack_id') or 'not checked')}</p>
  <table><thead><tr><th>Issue Type</th><th>Count</th></tr></thead><tbody>{triage_issue_rows or '<tr><td colspan="2">not checked</td></tr>'}</tbody></table>
  <h2>AI-HR Review Triage Source Artifacts</h2>
  <table><thead><tr><th>Input</th><th>Path</th></tr></thead><tbody>{triage_source_rows or '<tr><td colspan="2">not checked</td></tr>'}</tbody></table>
  <h2>Release Blockers</h2>
  <table><thead><tr><th>Category</th><th>Name</th><th>Message</th><th>Value</th><th>Threshold</th></tr></thead><tbody>{blocker_rows or '<tr><td colspan="5">none</td></tr>'}</tbody></table>
  <h2>Warnings</h2>
  <table><thead><tr><th>Name</th><th>Message</th><th>Value</th><th>Threshold</th></tr></thead><tbody>{warning_rows or '<tr><td colspan="4">none</td></tr>'}</tbody></table>
  <h2>Next Actions</h2>
  <table><thead><tr><th>Owner</th><th>Blocker</th><th>Action</th><th>Command</th></tr></thead><tbody>{next_action_rows or '<tr><td colspan="4">none</td></tr>'}</tbody></table>
  <h2>MCP Contract Checks</h2>
  <table><thead><tr><th>Name</th><th>OK</th><th>Detail</th></tr></thead><tbody>{mcp_check_rows or '<tr><td colspan="3">not checked</td></tr>'}</tbody></table>
  <h2>AI-HR Demo Contract</h2>
  <table><thead><tr><th>Artifact</th><th>OK</th><th>View</th><th>Rows/Length</th></tr></thead><tbody>{demo_rows or '<tr><td colspan="4">not checked</td></tr>'}</tbody></table>
  <h2>AI-HR Dashboard Surface</h2>
  <p class="muted">verification_artifact={_html_escape(dashboard_artifact.get('path') or 'not checked')}</p>
  <table><thead><tr><th>Scenario</th><th>OK</th><th>Rows</th><th>C1-2 Rows</th><th>C1-2 Review Required</th><th>C1-2 Approval Blocked</th><th>C1-2 Approval Safe</th><th>C2-2 Rows</th><th>C2-2 Hours</th><th>C2-2 Pending</th><th>C2-2 Approval Safe</th><th>Guide Trace</th><th>Missing Matrix</th><th>Missing Plan</th><th>Missing Guide Trace</th><th>Missing Query Route</th><th>Sensitive Markers</th></tr></thead><tbody>{dashboard_rows or '<tr><td colspan="17">not checked</td></tr>'}</tbody></table>
  <h2>AI-HR Review Chain Safety</h2>
  <table><thead><tr><th>Contract OK</th><th>Schema</th><th>Source Payload Exposed</th><th>Learning Module Items</th><th>NCS Report Items</th><th>OCR Cards</th><th>Blocked Automation Actions</th><th>Issues</th></tr></thead><tbody>{review_chain_rows or '<tr><td colspan="8">not checked</td></tr>'}</tbody></table>
  <h2>AI-HR Dashboard Static Artifacts</h2>
  <table><thead><tr><th>Name</th><th>Exists</th><th>Non Empty</th><th>Size</th><th>Path</th><th>Checkpoint OK</th><th>Checkpoint Schema</th><th>Checkpoint Summary</th></tr></thead><tbody>{static_artifact_rows or '<tr><td colspan="8">not checked</td></tr>'}</tbody></table>
</main>
</body>
</html>
"""


def render_aihr_agent_queue_html(payload: dict, source_path: Path) -> str:
    payload = sanitize_aihr_agent_queue_public_paths(copy.deepcopy(payload))
    items = payload.get("items") or []
    guardrails = payload.get("global_guardrails") or []
    guardrail_items = "".join(f"<li>{_html_escape(item)}</li>" for item in guardrails)
    item_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(item.get('priority'))}</td>"
        f"<td>{_html_escape(item.get('owner'))}<br><span class=\"muted\">{_html_escape(item.get('agent_file'))}</span></td>"
        f"<td>{_html_escape(item.get('blocker_display_label') or blocker_display_label(item.get('blocker')))}"
        f"<br><span class=\"muted\">{_html_escape(item.get('blocker'))}</span>"
        f"<br><span class=\"muted\">{_html_escape(item.get('blocker_category'))}</span></td>"
        f"<td>{_html_escape(item.get('mutation_policy'))}<br>auto={_html_escape(item.get('auto_runnable'))}<br>human={_html_escape(item.get('requires_human_decision'))}</td>"
        f"<td>{_html_escape(item.get('action'))}</td>"
        f"<td><code>{_html_escape(item.get('command'))}</code></td>"
        f"<td>{'<br>'.join(_html_escape(value) for value in (item.get('prerequisite_artifacts') or [])) or 'none'}</td>"
        f"<td>{'<br>'.join(_html_escape(value) for value in (item.get('prerequisite_commands') or [])) or 'none'}</td>"
        f"<td>{'<br>'.join(_html_escape(value) for value in (item.get('expected_artifacts') or [])) or 'none'}</td>"
        f"<td>{'<br>'.join(_html_escape(value) for value in (item.get('acceptance_checks') or []))}</td>"
        "</tr>"
        for item in items
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI-HR Agent Work Queue</title>
  <style>
    :root {{ --ink:#172033; --muted:#667085; --line:#d9e0ea; --bg:#f6f8fb; --panel:#fff; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Segoe UI, Arial, "Malgun Gothic", sans-serif; background:var(--bg); color:var(--ink); }}
    main {{ max-width:1400px; margin:0 auto; padding:24px; }}
    h1 {{ margin:0 0 6px; font-size:26px; }}
    h2 {{ margin:22px 0 8px; font-size:18px; }}
    .muted {{ color:var(--muted); }}
    .summary {{ display:grid; grid-template-columns:repeat(6, minmax(0,1fr)); gap:10px; margin-top:16px; }}
    .metric {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }}
    .metric span {{ display:block; color:var(--muted); font-size:12px; }}
    .metric strong {{ display:block; margin-top:4px; font-size:22px; }}
    table {{ width:100%; border-collapse:collapse; background:var(--panel); table-layout:fixed; }}
    th, td {{ border:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; overflow-wrap:anywhere; font-size:13px; }}
    th {{ background:#eef2f5; }}
    code {{ font-family:Consolas, monospace; font-size:12px; }}
    @media (max-width:760px) {{ main {{ padding:12px; }} .summary {{ grid-template-columns:1fr; }} table {{ display:block; overflow-x:auto; }} th, td {{ min-width:160px; }} }}
  </style>
</head>
<body>
<main>
  <p class="muted">{_html_escape(source_path.name)}</p>
  <h1>AI-HR Agent Work Queue</h1>
  <p class="muted">Subagent execution briefs generated from release-readiness blockers. This queue is not permission to approve human-review statuses.</p>
  <section class="summary">
    <div class="metric"><span>schema</span><strong>{_html_escape(payload.get('schema'))}</strong></div>
    <div class="metric"><span>release_ready</span><strong>{_html_escape(payload.get('release_ready'))}</strong></div>
    <div class="metric"><span>engineering_hygiene_ok</span><strong>{_html_escape(payload.get('engineering_hygiene_ok'))}</strong></div>
    <div class="metric"><span>items</span><strong>{_html_escape(payload.get('item_count') or len(items))}</strong></div>
  </section>
  <h2>Guardrails</h2>
  <ul>{guardrail_items or '<li>none</li>'}</ul>
  <h2>Queue Items</h2>
  <table>
    <thead><tr><th>Priority</th><th>Owner</th><th>Blocker</th><th>Policy</th><th>Action</th><th>Command</th><th>Prerequisites</th><th>Prerequisite Commands</th><th>Artifacts</th><th>Acceptance</th></tr></thead>
    <tbody>{item_rows or '<tr><td colspan="10">none</td></tr>'}</tbody>
  </table>
</main>
</body>
</html>
"""


def _render_agent_queue_status_rows(items: list[dict], *, empty_label: str) -> str:
    rows = []
    for item in items:
        prereqs = item.get("prerequisite_artifacts") or []
        missing_prereqs = item.get("missing_prerequisite_artifacts") or []
        safety = item.get("safety_violations") or []
        existing_outputs = item.get("existing_expected_artifacts") or []
        missing_outputs = item.get("missing_expected_artifacts") or []
        blockers = item.get("covered_blockers") or [item.get("blocker")]
        blocker_labels = item.get("covered_blocker_display_labels")
        if not isinstance(blocker_labels, list) or not blocker_labels:
            blocker_labels = blocker_display_labels(blockers)
        checks = item.get("acceptance_checks") or []
        rows.append(
            "<tr>"
            f"<td>{_html_escape(item.get('priority'))}<br><span class=\"muted\">{_html_escape(item.get('id'))}</span></td>"
            f"<td>{_html_escape(item.get('state'))}<br>auto={_html_escape(item.get('can_start_automated'))}<br>preflight={_html_escape(item.get('preflight_ok'))}</td>"
            f"<td>{_html_escape(item.get('owner'))}<br><span class=\"muted\">{_html_escape(item.get('agent_file'))}</span></td>"
            f"<td>{'<br>'.join(_html_escape(value) for value in blocker_labels if value) or 'none'}"
            f"<br><span class=\"muted\">{'<br>'.join(_html_escape(value) for value in blockers if value) or 'none'}</span></td>"
            f"<td>{_html_escape(item.get('mutation_policy'))}<br>human={_html_escape(item.get('requires_human_decision'))}</td>"
            f"<td><code>{_html_escape(item.get('command'))}</code></td>"
            f"<td>required: {'<br>'.join(_html_escape(value) for value in prereqs) or 'none'}<br>missing: {'<br>'.join(_html_escape(value) for value in missing_prereqs) or 'none'}</td>"
            f"<td>existing: {'<br>'.join(_html_escape(value) for value in existing_outputs) or 'none'}<br>missing: {'<br>'.join(_html_escape(value) for value in missing_outputs) or 'none'}</td>"
            f"<td>{'<br>'.join(_html_escape(value) for value in safety) or 'none'}</td>"
            f"<td>{'<br>'.join(_html_escape(value) for value in checks) or 'none'}</td>"
            "</tr>"
        )
    if rows:
        return "".join(rows)
    return f'<tr><td colspan="10">{_html_escape(empty_label)}</td></tr>'


def render_aihr_agent_queue_status_html(payload: dict, source_path: Path) -> str:
    payload = sanitize_aihr_agent_queue_public_paths(copy.deepcopy(payload))
    summary = payload.get("summary") or {}
    execution_order = payload.get("execution_order") or []
    manual_queue = payload.get("manual_queue") or []
    blocked_queue = payload.get("blocked_queue") or []
    items = payload.get("items") or []
    guardrails = payload.get("global_guardrails") or []
    guardrail_items = "".join(f"<li>{_html_escape(item)}</li>" for item in guardrails)
    state_counts = summary.get("state_counts") or {}
    state_items = "".join(
        f"<span>{_html_escape(key)}={_html_escape(value)}</span>" for key, value in sorted(state_counts.items())
    )
    execution_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(item.get('priority'))}</td>"
        f"<td>{_html_escape(item.get('owner'))}</td>"
        f"<td>{_html_escape(item.get('mutation_policy'))}<br>human={_html_escape(item.get('requires_human_decision'))}</td>"
        f"<td><code>{_html_escape(item.get('command'))}</code></td>"
        "</tr>"
        for item in execution_order
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI-HR Agent Queue Status</title>
  <style>
    :root {{ --ink:#172033; --muted:#667085; --line:#d9e0ea; --bg:#f6f8fb; --panel:#fff; --ok:#047857; --warn:#b45309; --bad:#b91c1c; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Segoe UI, Arial, "Malgun Gothic", sans-serif; background:var(--bg); color:var(--ink); }}
    main {{ max-width:1400px; margin:0 auto; padding:24px; }}
    h1 {{ margin:0 0 6px; font-size:26px; }}
    h2 {{ margin:22px 0 8px; font-size:18px; }}
    .muted {{ color:var(--muted); }}
    .summary {{ display:grid; grid-template-columns:repeat(7, minmax(0,1fr)); gap:10px; margin-top:16px; }}
    .metric {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }}
    .metric span {{ display:block; color:var(--muted); font-size:12px; }}
    .metric strong {{ display:block; margin-top:4px; font-size:22px; }}
    .ok {{ color:var(--ok); }} .warn {{ color:var(--warn); }} .bad {{ color:var(--bad); }}
    .tags {{ display:flex; flex-wrap:wrap; gap:6px; margin:10px 0; }}
    .tags span {{ border:1px solid var(--line); border-radius:999px; padding:5px 8px; background:#fff; font-size:12px; }}
    table {{ width:100%; border-collapse:collapse; background:var(--panel); table-layout:fixed; }}
    th, td {{ border:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; overflow-wrap:anywhere; font-size:13px; }}
    th {{ background:#eef2f5; }}
    code {{ font-family:Consolas, monospace; font-size:12px; }}
    @media (max-width:760px) {{ main {{ padding:12px; }} .summary {{ grid-template-columns:1fr; }} table {{ display:block; overflow-x:auto; }} th, td {{ min-width:160px; }} }}
  </style>
</head>
<body>
<main>
  <p class="muted">{_html_escape(source_path.name)} | source queue: {_html_escape(payload.get('source_queue_path'))}</p>
  <h1>AI-HR Agent Queue Status</h1>
  <p class="muted">Preflight view for automated subagent execution. Ready items can be started by automation; manual-ready items require an explicit human decision or guarded operator action.</p>
  <section class="summary">
    <div class="metric"><span>ok</span><strong class="{ 'ok' if payload.get('ok') else 'bad' }">{_html_escape(payload.get('ok'))}</strong></div>
    <div class="metric"><span>items</span><strong>{_html_escape(summary.get('item_count'))}</strong></div>
    <div class="metric"><span>auto_startable</span><strong class="{ 'ok' if summary.get('auto_startable_count') else 'warn' }">{_html_escape(summary.get('auto_startable_count'))}</strong></div>
    <div class="metric"><span>manual_ready</span><strong>{_html_escape(summary.get('manual_ready_count'))}</strong></div>
    <div class="metric"><span>manual_human_decision</span><strong>{_html_escape(summary.get('manual_human_decision_count'))}</strong></div>
    <div class="metric"><span>guarded_manual</span><strong>{_html_escape(summary.get('guarded_manual_count'))}</strong></div>
    <div class="metric"><span>blocked</span><strong class="{ 'bad' if summary.get('blocked_count') else 'ok' }">{_html_escape(summary.get('blocked_count'))}</strong></div>
  </section>
  <div class="tags">{state_items or '<span>no state counts</span>'}</div>
  <h2>Automated Start Order</h2>
  <table><thead><tr><th>Priority</th><th>Owner</th><th>Policy</th><th>Command</th></tr></thead><tbody>{execution_rows or '<tr><td colspan="4">No auto-startable items.</td></tr>'}</tbody></table>
  <h2>Manual Ready</h2>
  <table><thead><tr><th>Priority / ID</th><th>State</th><th>Owner</th><th>Covered Blockers</th><th>Policy</th><th>Command</th><th>Prereqs</th><th>Outputs</th><th>Safety</th><th>Acceptance</th></tr></thead><tbody>{_render_agent_queue_status_rows(manual_queue, empty_label='No manual-ready items.')}</tbody></table>
  <h2>Blocked</h2>
  <table><thead><tr><th>Priority / ID</th><th>State</th><th>Owner</th><th>Covered Blockers</th><th>Policy</th><th>Command</th><th>Prereqs</th><th>Outputs</th><th>Safety</th><th>Acceptance</th></tr></thead><tbody>{_render_agent_queue_status_rows(blocked_queue, empty_label='No blocked items.')}</tbody></table>
  <h2>All Items</h2>
  <table><thead><tr><th>Priority / ID</th><th>State</th><th>Owner</th><th>Covered Blockers</th><th>Policy</th><th>Command</th><th>Prereqs</th><th>Outputs</th><th>Safety</th><th>Acceptance</th></tr></thead><tbody>{_render_agent_queue_status_rows(items, empty_label='No queue status items.')}</tbody></table>
  <h2>Guardrails</h2>
  <ul>{guardrail_items or '<li>none</li>'}</ul>
</main>
</body>
</html>
"""


def render_aihr_agent_queue_run_html(payload: dict, source_path: Path) -> str:
    payload = sanitize_aihr_agent_queue_run_payload(payload)
    summary = payload.get("summary") or {}
    queue_summary = payload.get("queue_status_summary") or {}
    runs = payload.get("runs") or []

    def output_cell(item: dict, prefix: str) -> str:
        original_chars = item.get(f"{prefix}_original_chars")
        tail_chars = item.get(f"{prefix}_tail_chars")
        truncated = item.get(f"{prefix}_truncated")
        redacted = item.get(f"{prefix}_redacted")
        redaction_count = item.get(f"{prefix}_redaction_count") or 0
        parts: list[str] = []
        if original_chars is not None and tail_chars is not None:
            label = "truncated" if truncated else "complete"
            parts.append(
                f"{_html_escape(label)} {_html_escape(tail_chars)}/{_html_escape(original_chars)} chars"
            )
        parts.append(f"redacted={_html_escape(bool(redacted))}")
        parts.append(f"redactions={_html_escape(redaction_count)}")
        return f"<div class=\"muted\">{'; '.join(parts)}</div>"

    run_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(item.get('order'))}<br>{_html_escape(item.get('blocker_display_label') or item.get('id'))}"
        f"<br><span class=\"muted\">{_html_escape(item.get('id'))}</span></td>"
        f"<td>{_html_escape(item.get('status'))}<br>exit={_html_escape(item.get('exit_code'))}</td>"
        f"<td>{_html_escape(item.get('owner'))}<br><span class=\"muted\">{_html_escape(item.get('mutation_policy'))}</span></td>"
        f"<td>{_html_escape(item.get('started_at'))}<br><span class=\"muted\">{_html_escape(item.get('duration_seconds'))}s</span></td>"
        f"<td><code>{_html_escape(safe_aihr_agent_queue_run_command_label(item))}</code></td>"
        f"<td>{'<br>'.join(_html_escape(value) for value in (item.get('validation_errors') or [])) or 'none'}</td>"
        f"<td>{output_cell(item, 'stdout')}</td>"
        f"<td>{output_cell(item, 'stderr')}</td>"
        "</tr>"
        for item in runs
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI-HR Agent Queue Run</title>
  <style>
    :root {{ --ink:#172033; --muted:#667085; --line:#d9e0ea; --bg:#f6f8fb; --panel:#fff; --ok:#047857; --warn:#b45309; --bad:#b91c1c; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Segoe UI, Arial, "Malgun Gothic", sans-serif; background:var(--bg); color:var(--ink); }}
    main {{ max-width:1400px; margin:0 auto; padding:24px; }}
    h1 {{ margin:0 0 6px; font-size:26px; }}
    h2 {{ margin:22px 0 8px; font-size:18px; }}
    .muted {{ color:var(--muted); }}
    .summary {{ display:grid; grid-template-columns:repeat(6, minmax(0,1fr)); gap:10px; margin-top:16px; }}
    .metric {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }}
    .metric span {{ display:block; color:var(--muted); font-size:12px; }}
    .metric strong {{ display:block; margin-top:4px; font-size:22px; }}
    .ok {{ color:var(--ok); }} .warn {{ color:var(--warn); }} .bad {{ color:var(--bad); }}
    table {{ width:100%; border-collapse:collapse; background:var(--panel); table-layout:fixed; }}
    th, td {{ border:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; overflow-wrap:anywhere; font-size:13px; }}
    th {{ background:#eef2f5; }}
    code {{ font-family:Consolas, monospace; font-size:12px; }}
    pre {{ white-space:pre-wrap; margin:0; max-height:260px; overflow:auto; font-family:Consolas, monospace; font-size:12px; }}
    @media (max-width:760px) {{ main {{ padding:12px; }} .summary {{ grid-template-columns:1fr; }} table {{ display:block; overflow-x:auto; }} th, td {{ min-width:180px; }} }}
  </style>
</head>
<body>
<main>
  <p class="muted">{_html_escape(source_path.name)} | source queue: {_html_escape(payload.get('source_queue_path'))}</p>
  <h1>AI-HR Agent Queue Run</h1>
  <p class="muted">Execution evidence for preflight-approved reports-only queue items. Manual review and guarded API collection items are intentionally not executed here.</p>
  <section class="summary">
    <div class="metric"><span>ok</span><strong class="{ 'ok' if payload.get('ok') else 'bad' }">{_html_escape(payload.get('ok'))}</strong></div>
    <div class="metric"><span>selected</span><strong>{_html_escape(summary.get('selected_count'))}</strong></div>
    <div class="metric"><span>succeeded</span><strong class="{ 'ok' if summary.get('succeeded_count') else 'warn' }">{_html_escape(summary.get('succeeded_count'))}</strong></div>
    <div class="metric"><span>failed</span><strong class="{ 'bad' if summary.get('failed_count') else 'ok' }">{_html_escape(summary.get('failed_count'))}</strong></div>
    <div class="metric"><span>skipped unsafe</span><strong class="{ 'bad' if summary.get('skipped_unsafe_count') else 'ok' }">{_html_escape(summary.get('skipped_unsafe_count'))}</strong></div>
    <div class="metric"><span>queue auto-startable</span><strong>{_html_escape(queue_summary.get('auto_startable_count'))}</strong></div>
  </section>
  <h2>Runs</h2>
  <table><thead><tr><th>Order / ID</th><th>Status</th><th>Owner</th><th>Timing</th><th>Command</th><th>Validation</th><th>Stdout Tail</th><th>Stderr Tail</th></tr></thead><tbody>{run_rows or '<tr><td colspan="8">No automated run items.</td></tr>'}</tbody></table>
</main>
</body>
</html>
"""


def render_aihr_review_board_html(payload: dict, source_path: Path) -> str:
    payload = public_aihr_dashboard_payload(payload)
    summary = payload.get("summary") or {}
    warnings = payload.get("quality_warnings") or []
    transition_items = payload.get("transition_review_priorities") or []
    trust_candidates = payload.get("transition_trust_review_candidates") or []
    review_items = payload.get("review_priority_items") or []
    focus_overlays = payload.get("focus_review_priority_overlays") or []
    cross_checks = payload.get("cross_checks") or []
    constraints = payload.get("operator_constraints") or []
    transition_snapshot = summary.get("transition_status_snapshot") or {}
    source_paths = summary.get("source_paths") or {}
    actual_status_counts = transition_snapshot.get("actual_review_status_counts") or {}
    requested_statuses = transition_snapshot.get("requested_review_statuses") or []
    missing_requested_statuses = transition_snapshot.get("missing_requested_review_statuses") or []
    review_issue_type_counts = summary.get("review_issue_type_counts") or {}
    safety_contract = {
        "schema": payload.get("schema"),
        "report_only": payload.get("report_only"),
        "status_update_allowed": payload.get("status_update_allowed"),
        "db_writes": payload.get("db_writes"),
        "approval_claim": payload.get("approval_claim"),
        "human_decision_required": payload.get("human_decision_required"),
    }
    safety_issues = []
    if payload.get("schema") != "ncs_review_triage_v1":
        safety_issues.append("schema_not_ncs_review_triage_v1")
    if payload.get("report_only") is not True:
        safety_issues.append("report_only_not_true")
    for field_name in ("status_update_allowed", "db_writes", "approval_claim"):
        if payload.get(field_name) is not False:
            safety_issues.append(f"{field_name}_not_false")
    safety_contract_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(key)}</td>"
        f"<td>{_html_escape(value)}</td>"
        "</tr>"
        for key, value in safety_contract.items()
    )
    warning_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(item.get('category'))}</td>"
        f"<td>{_html_escape(item.get('name'))}</td>"
        f"<td>{_html_escape(item.get('message'))}</td>"
        f"<td>{_html_escape(item.get('value'))}</td>"
        f"<td>{_html_escape(item.get('action'))}</td>"
        "</tr>"
        for item in warnings
    )
    transition_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(item.get('rank'))}</td>"
        f"<td>{_html_escape(item.get('scenario_name'))}<br><span class=\"muted\">{_html_escape(item.get('review_status'))}</span></td>"
        f"<td>{_html_escape(item.get('current_query'))} -> {_html_escape(item.get('target_query'))}</td>"
        f"<td>{_html_escape(item.get('expected_recall_at_k'))}</td>"
        f"<td>{_html_escape(item.get('precision_at_k'))}</td>"
        f"<td>{_html_escape(json.dumps(item.get('course_scope_fit_relation_counts') or {}, ensure_ascii=False, sort_keys=True))}<br><span class=\"muted\">scope review: {_html_escape(item.get('course_scope_review_required_count'))}</span></td>"
        f"<td>{_html_escape(', '.join(item.get('flags') or []))}</td>"
        "</tr>"
        for item in transition_items
    )
    trust_candidate_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(item.get('rank'))}</td>"
        f"<td>{_html_escape(item.get('scenario_name'))}<br><span class=\"muted\">{_html_escape(item.get('review_status'))}</span></td>"
        f"<td>{_html_escape(item.get('candidate_score'))}</td>"
        f"<td>{_html_escape(item.get('review_readiness'))}</td>"
        f"<td>{_html_escape(item.get('expected_recall_at_k'))}</td>"
        f"<td>{_html_escape(item.get('precision_at_k'))}</td>"
        f"<td>{_html_escape(item.get('top1_expected_hit'))}</td>"
        f"<td>{_html_escape(item.get('direct_or_near_course_ratio'))}</td>"
        f"<td>{_html_escape(item.get('course_scope_review_required_count'))}</td>"
        f"<td>{_html_escape(item.get('decision_policy'))}</td>"
        "</tr>"
        for item in trust_candidates
        if isinstance(item, dict)
    )
    review_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(item.get('rank'))}</td>"
        f"<td>{_html_escape(item.get('issue_type'))}<br><span class=\"muted\">{_html_escape(item.get('target_type'))}:{_html_escape(item.get('target_id'))}</span></td>"
        f"<td>{_html_escape(item.get('severity'))}</td>"
        f"<td>{_html_escape(item.get('priority_score'))}</td>"
        f"<td>{_html_escape(item.get('priority_reason'))}</td>"
        f"<td>{_html_escape(item.get('context_excerpt'))}</td>"
        f"<td>{_html_escape(item.get('suggested_action'))}</td>"
        "</tr>"
        for item in review_items
    )
    focus_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(overlay.get('label') or overlay.get('code'))}<br><span class=\"muted\">{_html_escape(overlay.get('reason'))}</span></td>"
        f"<td>{_html_escape(overlay.get('major_code'))}</td>"
        f"<td>{_html_escape(overlay.get('item_count'))}</td>"
        f"<td>{_html_escape('; '.join(str((item or {}).get('issue_type') or '') + ':' + str((item or {}).get('context_excerpt') or '') for item in (overlay.get('items') or [])[:5]))}</td>"
        "</tr>"
        for overlay in focus_overlays
        if isinstance(overlay, dict)
    )
    cross_check_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(item.get('name'))}</td>"
        f"<td>{_html_escape(item.get('status'))}</td>"
        f"<td>{_html_escape(item.get('value'))}</td>"
        f"<td>{_html_escape(item.get('threshold'))}</td>"
        f"<td>{_html_escape(item.get('message'))}</td>"
        "</tr>"
        for item in cross_checks
    )
    source_path_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(key)}</td>"
        f"<td>{_html_escape(value)}</td>"
        "</tr>"
        for key, value in sorted(source_paths.items())
    )
    status_count_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(key)}</td>"
        f"<td>{_html_escape(value)}</td>"
        "</tr>"
        for key, value in sorted(actual_status_counts.items())
    )
    review_issue_type_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(key)}</td>"
        f"<td>{_html_escape(value)}</td>"
        "</tr>"
        for key, value in sorted(review_issue_type_counts.items())
    )
    constraint_items = "".join(f"<li>{_html_escape(item)}</li>" for item in constraints)
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI-HR 검토보드</title>
  <style>
    :root {{ --ink:#172033; --muted:#667085; --line:#d9e0ea; --bg:#f6f8fb; --panel:#fff; --warn:#b45309; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Segoe UI, Arial, "Malgun Gothic", sans-serif; background:var(--bg); color:var(--ink); }}
    main {{ max-width:1300px; margin:0 auto; padding:24px; }}
    h1 {{ margin:0 0 6px; font-size:26px; }}
    h2 {{ margin:22px 0 8px; font-size:18px; }}
    .muted {{ color:var(--muted); }}
    .summary {{ display:grid; grid-template-columns:repeat(6, minmax(0,1fr)); gap:10px; margin-top:16px; }}
    .metric {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }}
    .metric span {{ display:block; color:var(--muted); font-size:12px; }}
    .metric strong {{ display:block; margin-top:4px; font-size:22px; }}
    .notice {{ border-left:4px solid var(--warn); background:#fff8eb; padding:10px 12px; margin:14px 0; }}
    table {{ width:100%; border-collapse:collapse; background:var(--panel); table-layout:fixed; }}
    th, td {{ border:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; overflow-wrap:anywhere; font-size:13px; }}
    th {{ background:#eef2f5; }}
    @media (max-width:760px) {{ main {{ padding:12px; }} .summary {{ grid-template-columns:1fr; }} table {{ display:block; overflow-x:auto; }} th, td {{ min-width:150px; }} }}
  </style>
</head>
<body>
<main>
  <p class="muted">{_html_escape(source_path.name)}</p>
  <h1>AI-HR 검토보드</h1>
  <p class="muted">release_ready=false를 해소하기 위한 human review, transition scenario, collection-stability 항목을 한 화면에서 확인합니다.</p>
  <section class="summary">
    <div class="metric"><span>quality warnings</span><strong>{_html_escape(summary.get('quality_warning_count'))}</strong></div>
    <div class="metric"><span>review priority items</span><strong>{_html_escape(summary.get('review_priority_item_count'))}</strong></div>
    <div class="metric"><span>transition seedpack items</span><strong>{_html_escape(summary.get('transition_seedpack_item_count'))}</strong></div>
    <div class="metric"><span>transition attention</span><strong>{_html_escape(summary.get('transition_attention_count'))}</strong></div>
    <div class="metric"><span>trust review candidates</span><strong>{_html_escape(summary.get('transition_trust_review_candidate_count'))}</strong></div>
    <div class="metric"><span>trusted in seedpack</span><strong>{_html_escape(transition_snapshot.get('trusted_review_status_count'))}</strong></div>
    <div class="metric"><span>seedpack id</span><strong>{_html_escape(summary.get('transition_seedpack_id'))}</strong></div>
  </section>
  <div class="notice"><strong>운영 원칙</strong><ul>{constraint_items}</ul></div>
  <h2>Safety Contract</h2>
  <table><thead><tr><th>Field</th><th>Value</th></tr></thead><tbody>
    {safety_contract_rows}
    <tr><td>safety_issues</td><td>{_html_escape(', '.join(safety_issues) or 'none')}</td></tr>
  </tbody></table>
  <h2>Transition Review Batch</h2>
  <table><thead><tr><th>Field</th><th>Value</th></tr></thead><tbody>
    <tr><td>requested_review_statuses</td><td>{_html_escape(', '.join(str(value) for value in requested_statuses) or 'none')}</td></tr>
    <tr><td>missing_requested_review_statuses</td><td>{_html_escape(', '.join(str(value) for value in missing_requested_statuses) or 'none')}</td></tr>
    <tr><td>trusted_review_status_count</td><td>{_html_escape(transition_snapshot.get('trusted_review_status_count'))}</td></tr>
    {status_count_rows or '<tr><td>actual_review_status_counts</td><td>none</td></tr>'}
  </tbody></table>
  <h2>Source Artifacts</h2>
  <table><thead><tr><th>Input</th><th>Path</th></tr></thead><tbody>{source_path_rows or '<tr><td colspan="2">none</td></tr>'}</tbody></table>
  <h2>Review Issue Type Counts</h2>
  <table><thead><tr><th>Issue Type</th><th>Count</th></tr></thead><tbody>{review_issue_type_rows or '<tr><td colspan="2">none</td></tr>'}</tbody></table>
  <h2>Quality Warning Triage</h2>
  <table><thead><tr><th>Category</th><th>Name</th><th>Message</th><th>Value</th><th>Action</th></tr></thead><tbody>{warning_rows or '<tr><td colspan="5">none</td></tr>'}</tbody></table>
  <h2>Transition Scenario Review Priority</h2>
  <table><thead><tr><th>Rank</th><th>Scenario</th><th>Query</th><th>Recall</th><th>Precision</th><th>Course Scope Fit</th><th>Flags</th></tr></thead><tbody>{transition_rows or '<tr><td colspan="7">none</td></tr>'}</tbody></table>
  <h2>Transition Trust Review Candidates</h2>
  <table><thead><tr><th>Rank</th><th>Scenario</th><th>Score</th><th>Readiness</th><th>Recall</th><th>Precision</th><th>Top1</th><th>Direct/Near</th><th>Scope Review</th><th>Policy</th></tr></thead><tbody>{trust_candidate_rows or '<tr><td colspan="10">none</td></tr>'}</tbody></table>
  <h2>Cross Checks</h2>
  <table><thead><tr><th>Name</th><th>Status</th><th>Value</th><th>Threshold</th><th>Message</th></tr></thead><tbody>{cross_check_rows or '<tr><td colspan="5">none</td></tr>'}</tbody></table>
  <h2>Focus Review Priority Overlays</h2>
  <table><thead><tr><th>Overlay</th><th>Major</th><th>Items</th><th>Top Context</th></tr></thead><tbody>{focus_rows or '<tr><td colspan="4">none</td></tr>'}</tbody></table>
  <h2>Ontology / Training Evidence Review Priority</h2>
  <table><thead><tr><th>Rank</th><th>Target</th><th>Severity</th><th>Score</th><th>Reason</th><th>Context</th><th>Suggested Action</th></tr></thead><tbody>{review_rows or '<tr><td colspan="7">none</td></tr>'}</tbody></table>
</main>
</body>
</html>
"""


HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NCS-SQF Ontology Workbench</title>
  <style>
    :root {
      --bg:#f5f7fb; --panel:#fff; --ink:#172033; --muted:#667085;
      --line:#d9e0ea; --dark:#111827; --accent:#2563eb; --ok:#047857;
      --warn:#b45309; --bad:#b91c1c;
    }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Segoe UI, Arial, sans-serif; background:var(--bg); color:var(--ink); }
    header { background:var(--dark); color:#fff; padding:18px 24px; display:flex; justify-content:space-between; align-items:center; }
    header h1 { margin:0; font-size:20px; }
    main { max-width:1600px; margin:0 auto; padding:20px 24px 44px; }
    section { margin-top:18px; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; }
    .toolbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:10px; }
    input, select, button, textarea { border:1px solid var(--line); border-radius:6px; padding:8px 10px; background:#fff; font:inherit; }
    input.code { width:72px; }
    input.keyword { width:220px; }
    button { cursor:pointer; background:#1f2937; color:#fff; border-color:#1f2937; }
    button.secondary { background:#fff; color:#1f2937; }
    button.link { background:#fff; color:var(--accent); border-color:#c7d2fe; }
    a.button-link { display:inline-block; text-decoration:none; border:1px solid #c7d2fe; border-radius:6px; padding:8px 10px; background:#fff; color:var(--accent); }
    .muted { color:var(--muted); }
    .ok { color:var(--ok); font-weight:600; }
    .warn { color:var(--warn); font-weight:600; }
    .bad { color:var(--bad); font-weight:600; }
    .cards { display:grid; gap:12px; grid-template-columns:repeat(5, minmax(0, 1fr)); }
    .card { background:#fff; border:1px solid var(--line); border-radius:8px; padding:13px; cursor:pointer; min-height:112px; }
    .card:hover { border-color:var(--accent); box-shadow:0 1px 8px rgba(37,99,235,.14); }
    .card.active { border-color:var(--accent); outline:2px solid rgba(37,99,235,.18); }
    .card .label { color:var(--muted); font-size:12px; }
    .card .value { font-size:26px; font-weight:700; margin:4px 0; }
    .card .sub { color:var(--muted); font-size:12px; line-height:1.35; }
    .split { display:grid; gap:14px; grid-template-columns:minmax(0, 1.45fr) minmax(440px, .85fr); align-items:start; }
    .scroll { overflow:auto; max-height:620px; border:1px solid var(--line); border-radius:8px; }
    table { width:100%; border-collapse:collapse; font-size:13px; background:#fff; }
    th, td { border-bottom:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; }
    th { position:sticky; top:0; background:#f9fafb; z-index:1; }
    tr:hover td { background:#fbfdff; }
    .pill { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:2px 8px; font-size:12px; background:#fff; }
    .detail-box { white-space:pre-wrap; background:#f9fafb; border:1px solid var(--line); border-radius:8px; padding:12px; margin-top:10px; max-height:220px; overflow:auto; }
    textarea { width:100%; min-height:210px; min-width:420px; line-height:1.55; resize:vertical; }
    textarea.small { min-height:84px; }
    .field { margin-top:12px; }
    .field label { display:block; font-size:13px; color:var(--muted); margin-bottom:5px; }
    .summary { display:grid; gap:12px; grid-template-columns:repeat(6, minmax(150px,1fr)); }
    .summary .panel { min-height:88px; }
    .summary strong { display:block; font-size:24px; margin-top:4px; }
    .taxonomy-head { display:flex; justify-content:space-between; gap:16px; align-items:flex-end; margin-bottom:12px; }
    .taxonomy-head h2 { margin:0; font-size:18px; }
    .taxonomy-head p { margin:4px 0 0; color:var(--muted); }
    .major-grid { display:grid; grid-template-columns:repeat(8, minmax(0,1fr)); gap:10px; }
    .major-tile { min-height:112px; text-align:center; color:var(--ink); background:#fff; border:1px solid var(--line); border-radius:8px; padding:12px 9px; cursor:pointer; }
    .major-tile:hover, .node:hover { border-color:var(--accent); box-shadow:0 1px 8px rgba(37,99,235,.12); }
    .major-tile.active, .node.active { border-color:var(--accent); outline:2px solid rgba(37,99,235,.18); background:#f8fbff; }
    .major-icon { width:42px; height:42px; display:grid; place-items:center; border-radius:8px; background:#e0f2fe; color:#075985; font-size:22px; margin:0 auto 8px; }
    .node-icon { width:28px; height:28px; display:grid; place-items:center; border-radius:7px; background:#f1f5f9; color:#334155; font-size:13px; font-weight:700; flex:0 0 auto; }
    .tile-title { font-weight:700; line-height:1.25; min-height:34px; }
    .tile-meta { font-size:12px; color:var(--muted); margin-top:5px; }
    .progress { height:7px; background:#e5e7eb; border-radius:999px; overflow:hidden; margin-top:8px; }
    .progress > span { display:block; height:100%; background:linear-gradient(90deg,#0ea5e9,#2563eb); width:0; }
    .hierarchy-grid { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); border:1px solid var(--line); border-radius:8px; overflow:hidden; margin-top:14px; background:#fff; }
    .lane { min-height:360px; border-right:1px solid var(--line); }
    .lane:last-child { border-right:0; }
    .lane h3 { margin:0; padding:12px; font-size:14px; background:#f8fafc; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; gap:8px; }
    .lane-list { max-height:440px; overflow:auto; padding:8px; display:grid; gap:7px; }
    .node { width:100%; color:var(--ink); background:#fff; border:1px solid var(--line); border-radius:8px; padding:9px; cursor:pointer; text-align:left; display:flex; gap:9px; align-items:flex-start; }
    .node-main { min-width:0; flex:1; }
    .node-title { display:block; font-weight:700; line-height:1.3; overflow-wrap:anywhere; }
    .node-sub { display:block; font-size:12px; color:var(--muted); margin-top:2px; overflow-wrap:anywhere; }
    .sub-status { margin-top:14px; display:none; border:1px solid var(--line); border-radius:8px; background:#fbfdff; padding:14px; }
    .sub-status.visible { display:block; }
    .sub-status h3 { margin:0 0 10px; }
    .status-grid { display:grid; grid-template-columns:repeat(5, minmax(0,1fr)); gap:10px; }
    .status-cell { background:#fff; border:1px solid var(--line); border-radius:8px; padding:10px; min-height:82px; }
    .status-cell b { display:block; margin-bottom:4px; }
    .quick-actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
    .ontology-tree { display:grid; gap:10px; }
    .ontology-unit { border:1px solid var(--line); border-radius:8px; background:#fff; overflow:hidden; }
    .ontology-unit > summary { cursor:pointer; padding:12px; font-weight:700; background:#f8fafc; }
    .ontology-body { padding:10px 12px 14px; display:grid; gap:10px; }
    .ontology-element { border-left:3px solid #0ea5e9; padding:8px 0 8px 10px; }
    .ontology-element h4 { margin:0 0 8px; font-size:14px; }
    .ontology-columns { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:10px; }
    .ontology-group { border:1px solid var(--line); border-radius:8px; padding:8px; background:#fbfdff; }
    .ontology-group h5 { margin:0 0 7px; font-size:13px; }
    .ontology-list { display:grid; gap:6px; }
    .ontology-item { width:100%; text-align:left; color:var(--ink); background:#fff; border:1px solid var(--line); border-radius:6px; padding:7px; cursor:pointer; }
    .ontology-item:hover { border-color:var(--accent); }
    .ontology-item .muted { display:block; margin-top:2px; }
    .ontology-status { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:10px; }
    .ontology-stat { border:1px solid var(--line); border-radius:8px; padding:12px; background:#fff; }
    .ontology-stat h3 { margin:0 0 8px; font-size:15px; }
    .ontology-stat button { margin:4px 4px 0 0; }
    details.advanced { margin-top:18px; }
    details.advanced > summary { cursor:pointer; font-weight:700; background:#fff; border:1px solid var(--line); border-radius:8px; padding:12px 14px; }
    details.advanced[open] > summary { border-bottom-left-radius:0; border-bottom-right-radius:0; }
    .guide-row td { background:#fbfdff; color:var(--muted); padding:22px; text-align:center; }
    @media (max-width:1180px) {
      .cards { grid-template-columns:repeat(3, minmax(0,1fr)); }
      .major-grid { grid-template-columns:repeat(4, minmax(0,1fr)); }
      .hierarchy-grid { grid-template-columns:repeat(2, minmax(0,1fr)); }
      .ontology-columns { grid-template-columns:1fr; }
      .lane:nth-child(2) { border-right:0; }
      .lane:nth-child(1), .lane:nth-child(2) { border-bottom:1px solid var(--line); }
      .split { grid-template-columns:1fr; }
    }
    @media (max-width:720px) {
      .cards, .summary, .major-grid, .hierarchy-grid, .status-grid { grid-template-columns:1fr; }
      .lane { border-right:0; border-bottom:1px solid var(--line); }
      .lane:last-child { border-bottom:0; }
      textarea { min-width:280px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>NCS-SQF 온톨로지 워크벤치</h1>
    <div id="stamp" class="muted"></div>
  </header>
  <main>
    <section class="panel">
      <div class="toolbar">
        <strong>범위</strong>
        <input id="majorCode" class="code" value="" title="대분류코드" placeholder="대">
        <input id="middleCode" class="code" value="" title="중분류코드" placeholder="중">
        <input id="smallCode" class="code" value="" title="소분류코드" placeholder="소">
        <input id="subCode" class="code" value="" title="세분류코드" placeholder="세">
        <input id="keyword" class="keyword" placeholder="능력단위/요소/문장 검색">
        <button onclick="refreshAll()">조회</button>
        <button class="secondary" onclick="clearScope()">전체 NCS</button>
        <button class="secondary" onclick="setHrScope()">인사 직무</button>
        <button class="secondary" onclick="setManagementSupportMvp()">경영지원 MVP</button>
        <a class="button-link" href="/ncs-knowledge-graph" target="_blank" rel="noopener">NCS Knowledge Graph</a>
        <a class="button-link" href="/aihr-live" target="_blank" rel="noopener">AI-HR Live</a>
        <a class="button-link" href="/aihr-training-system-builder" target="_blank" rel="noopener">Training Builder</a>
        <a class="button-link" href="/aihr-plan-demo" target="_blank" rel="noopener">AI-HR 데모</a>
        <a class="button-link" href="/aihr-readiness" target="_blank" rel="noopener">AI-HR 준비도</a>
        <a class="button-link" href="/aihr-review-board" target="_blank" rel="noopener">AI-HR 검토보드</a>
        <a class="button-link" href="/ontology-review-board" target="_blank" rel="noopener">Ontology Review</a>
        <a class="button-link" href="/ksa-review-dashboard" target="_blank" rel="noopener">KSA Review</a>
        <a class="button-link" href="/ksa-label-patterns" target="_blank" rel="noopener">KSA Label Patterns</a>
        <a class="button-link" href="/ksa-preprocessing-dashboard" target="_blank" rel="noopener">KSA Preprocessing</a>
        <a class="button-link" href="/aihr-query-router" target="_blank" rel="noopener">쿼리 라우터</a>
        <a class="button-link" href="/aihr-provenance-reconfirmation" target="_blank" rel="noopener">Provenance Reconfirm</a>
        <a class="button-link" href="/aihr-agent-queue" target="_blank" rel="noopener">Agent Queue</a>
        <a class="button-link" href="/aihr-agent-queue-status" target="_blank" rel="noopener">Queue Status</a>
        <a class="button-link" href="/aihr-agent-queue-run" target="_blank" rel="noopener">Queue Run</a>
        <span id="liveStatus" class="muted"></span>
      </div>
      <div class="muted">대분류 아이콘을 누르고 중분류, 소분류, 세분류를 차례로 선택하세요. 경영지원 MVP는 SQF `02 > 경영관리 > 경영지원`을 우선 범위로 보고 NCS `02 경영·회계·사무`와 연결합니다.</div>
    </section>

    <section class="summary" id="summary"></section>

    <section class="panel">
      <div class="taxonomy-head">
        <div>
          <h2>NCS 분류 클릭 탐색</h2>
          <p>대분류 아이콘을 누른 뒤 중분류, 소분류, 세분류를 차례로 선택하면 해당 세분류의 전처리 현황과 수작업 보정 대상이 바로 열립니다.</p>
        </div>
        <button class="secondary" onclick="resetTaxonomy()">대분류 다시 선택</button>
      </div>
      <div id="majorTiles" class="major-grid"></div>
      <div class="hierarchy-grid">
        <div class="lane">
          <h3>중분류 <span id="middleMeta" class="muted"></span></h3>
          <div id="middleList" class="lane-list"></div>
        </div>
        <div class="lane">
          <h3>소분류 <span id="smallMeta" class="muted"></span></h3>
          <div id="smallList" class="lane-list"></div>
        </div>
        <div class="lane">
          <h3>세분류 <span id="subMeta" class="muted"></span></h3>
          <div id="subList" class="lane-list"></div>
        </div>
        <div class="lane">
          <h3>능력단위 <span id="unitMeta" class="muted"></span></h3>
          <div id="unitList" class="lane-list"></div>
        </div>
      </div>
      <div id="subStatus" class="sub-status"></div>
    </section>

    <section class="panel">
      <div class="toolbar">
        <strong>선택 범위 온톨로지 구조</strong>
        <span id="ontologyMeta" class="muted"></span>
        <button class="secondary" onclick="loadOntology()">새로고침</button>
      </div>
      <div class="toolbar">
        <strong>온톨로지 구축 워크벤치</strong>
        <span id="ontologyWorkbenchMeta" class="muted"></span>
      </div>
      <div id="ontologyWorkbench" class="ontology-status"></div>
      <div class="scroll" style="max-height:260px; margin:12px 0;">
        <table>
          <thead>
            <tr><th>개념</th><th>유형</th><th>정의</th><th>관계</th><th>별칭</th><th>작업</th></tr>
          </thead>
          <tbody id="conceptWorkItems">
            <tr class="guide-row"><td colspan="6">온톨로지 작업 버튼을 클릭하세요.</td></tr>
          </tbody>
        </table>
      </div>
      <div id="ontologyTree" class="ontology-tree">
        <div class="muted">대분류를 선택하면 능력단위-요소-수행준거-KSA 구조가 표시됩니다.</div>
      </div>
    </section>

    <section class="panel">
      <div class="toolbar">
        <strong>Recommendation Audit</strong>
        <button class="secondary" onclick="loadRecommendationRuns()">Reload</button>
        <span id="recommendationMeta" class="muted"></span>
      </div>
      <div class="split">
        <div class="scroll" style="max-height:320px;">
          <table>
            <thead>
              <tr><th>Run</th><th>Query</th><th>Target</th><th>Summary</th><th>Action</th></tr>
            </thead>
            <tbody id="recommendationRuns">
              <tr class="guide-row"><td colspan="5">No recommendation runs loaded.</td></tr>
            </tbody>
          </table>
        </div>
        <div id="recommendationDetail" class="detail-box">Select a recommendation run.</div>
      </div>
    </section>

    <details class="advanced">
      <summary>상세 진행 현황 / 원시 상태 카드 보기</summary>
      <section class="panel" style="border-top:0; border-top-left-radius:0; border-top-right-radius:0;">
        <div class="toolbar">
          <strong>온톨로지 준비 전처리 단계</strong>
          <span class="muted">선택된 분류 범위의 완료/잔여 작업과 산출 방식을 확인합니다.</span>
        </div>
        <div class="scroll" style="max-height:360px;">
          <table>
            <thead>
              <tr><th>단계</th><th>의미</th><th>완료</th><th>남은 작업</th><th>방법/산출물</th><th>보기</th></tr>
            </thead>
            <tbody id="phases"></tbody>
          </table>
        </div>
      </section>
      <section class="cards" id="cards"></section>
    </details>

    <section class="split">
      <div class="panel">
        <div class="toolbar">
          <strong id="listTitle">전처리 항목</strong>
          <span id="listMeta" class="muted"></span>
          <button class="secondary" onclick="loadCurrentItems()">새로고침</button>
        </div>
        <div class="scroll">
          <table>
            <thead>
              <tr><th>상태</th><th>코드/ID</th><th>분류/맥락</th><th>원문/내용</th><th>작업</th></tr>
            </thead>
            <tbody id="items"></tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <div class="toolbar">
          <strong>상세 / 수작업 전처리</strong>
          <span id="detailKind" class="muted"></span>
        </div>
        <div id="emptyDetail" class="muted">왼쪽에서 항목을 선택하세요.</div>
        <div id="detail" style="display:none;">
          <div class="field">
            <label>현재 상태</label>
            <div id="detailStatus" class="detail-box"></div>
          </div>
          <div class="field">
            <label>맥락</label>
            <div id="context" class="detail-box"></div>
          </div>
          <div class="field">
            <label id="titleRawLabel">원문 명칭</label>
            <div id="titleRaw" class="detail-box"></div>
          </div>
          <div class="field" id="titleEditWrap">
            <label id="titleRefinedLabel">정제 명칭</label>
            <textarea id="titleRefined" class="small"></textarea>
          </div>
          <div class="field">
            <label id="bodyRawLabel">원문/정의/내용</label>
            <div id="bodyRaw" class="detail-box"></div>
          </div>
          <div class="field" id="bodyEditWrap">
            <label id="bodyRefinedLabel">정제 내용</label>
            <textarea id="bodyRefined"></textarea>
          </div>
          <div id="ontologyConceptFields" style="display:none;">
            <div class="field">
              <label>별칭</label>
              <textarea id="conceptAliases" class="small" placeholder="한 줄에 하나씩 입력"></textarea>
            </div>
            <div class="field">
              <label>상위 개념</label>
              <textarea id="parentConcepts" class="small" placeholder="한 줄에 하나씩 입력"></textarea>
            </div>
            <div class="field">
              <label>하위 개념</label>
              <textarea id="childConcepts" class="small" placeholder="한 줄에 하나씩 입력"></textarea>
            </div>
            <div class="field">
              <label>관련 개념</label>
              <textarea id="relatedConcepts" class="small" placeholder="한 줄에 하나씩 입력"></textarea>
            </div>
            <div class="field">
              <label>관련 수행준거</label>
              <div id="relatedCriteria" class="detail-box"></div>
            </div>
          </div>
          <div class="field">
            <label>검토자 ID</label>
            <input id="manualReviewerId" placeholder="예: user_directed_report_review_20260619" />
          </div>
          <div class="field">
            <label>검토 패킷</label>
            <input id="manualSourcePacket" placeholder="reports/..." />
          </div>
          <div class="field">
            <label>source artifact SHA-256</label>
            <input id="manualSourceHash" placeholder="sha256:..." />
          </div>
          <div class="field">
            <label>판단 근거</label>
            <textarea id="manualRationale" class="small"></textarea>
          </div>
          <div class="field">
            <label>확인 근거 참조</label>
            <textarea id="manualEvidenceRefs" class="small" placeholder="한 줄에 하나씩 입력"></textarea>
          </div>
          <div class="toolbar" style="margin-top:12px;">
            <button onclick="saveCurrentDetail()">수작업 전처리 저장</button>
            <button class="secondary" onclick="fillRawAsRefined()">원문 그대로 사용</button>
            <button class="secondary" onclick="loadCurrentItems()">새로고침</button>
          </div>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="toolbar">
        <strong>품질 이슈</strong>
        <select id="targetType">
          <option value="">전체 대상</option>
          <option value="criteria">수행준거</option>
          <option value="ksa">KSA</option>
          <option value="element">능력단위요소</option>
          <option value="unit">능력단위</option>
        </select>
        <select id="issueType"></select>
        <button onclick="loadIssues()">이슈 조회</button>
      </div>
      <div class="scroll">
        <table>
          <thead>
            <tr><th>ID</th><th>유형</th><th>대상</th><th>심각도</th><th>내용</th><th>작업</th></tr>
          </thead>
          <tbody id="issues"></tbody>
        </table>
      </div>
    </section>
  </main>

  <script>
    const fmt = new Intl.NumberFormat('ko-KR');
    const q = (id) => document.getElementById(id);
    let currentCard = {kind:'criteria', state:'raw', title:'수행준거 미정제'};
    let currentDetail = null;
    const majorIcons = {
      '01':'📊','02':'🧾','03':'🏦','04':'🎓','05':'⚖️','06':'🏥',
      '07':'🤝','08':'🎨','09':'🚚','10':'🏷️','11':'🧹','12':'🏨',
      '13':'🍽️','14':'🏗️','15':'⚙️','16':'🧱','17':'🧪','18':'🧵',
      '19':'⚡','20':'📡','21':'🥫','22':'🖨️','23':'🌱','24':'🚜'
    };
    let overviewTimer = null;
    const fallbackMajorNodes = [
      ['01','사업관리'], ['02','경영·회계·사무'], ['03','금융·보험'], ['04','교육·자연·사회과학'],
      ['05','법률·경찰·소방·교도·국방'], ['06','보건·의료'], ['07','사회복지·종교'], ['08','문화·예술·디자인·방송'],
      ['09','운전·운송'], ['10','영업판매'], ['11','경비·청소'], ['12','이용·숙박·여행·오락·스포츠'],
      ['13','음식서비스'], ['14','건설'], ['15','기계'], ['16','재료'],
      ['17','화학·바이오'], ['18','섬유·의복'], ['19','전기·전자'], ['20','정보통신'],
      ['21','식품가공'], ['22','인쇄·목재·가구·공예'], ['23','환경·에너지·안전'], ['24','농림어업']
    ].map(([major_code, name]) => ({major_code, middle_code:'', small_code:'', sub_code:'', code:major_code, name}));

    async function api(path, options={}) {
      const res = await fetch(path, options);
      if (!res.ok) throw new Error(await res.text());
      return await res.json();
    }
    function esc(text) {
      return String(text ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function scopeParams(includeKeyword=true) {
      const params = new URLSearchParams();
      for (const [id, key] of [['majorCode','major_code'], ['middleCode','middle_code'], ['smallCode','small_code'], ['subCode','sub_code']]) {
        const v = q(id).value.trim();
        if (v) params.set(key, v);
      }
      const kw = q('keyword').value.trim();
      if (includeKeyword && kw) params.set('keyword', kw);
      params.set('limit', '100');
      return params;
    }
    function clearScope() {
      for (const id of ['majorCode', 'middleCode', 'smallCode', 'subCode']) q(id).value = '';
      q('keyword').value = '';
      currentCard = {kind:'criteria', state:'raw', title:'수행준거 미정제'};
      refreshAll();
    }
    function resetTaxonomy() {
      clearScope();
    }
    function setScope(major='', middle='', small='', sub='') {
      q('majorCode').value = major || '';
      q('middleCode').value = middle || '';
      q('smallCode').value = small || '';
      q('subCode').value = sub || '';
    }
    function selectedCodes() {
      return {
        major: q('majorCode').value.trim(),
        middle: q('middleCode').value.trim(),
        small: q('smallCode').value.trim(),
        sub: q('subCode').value.trim()
      };
    }
    function hasSelectedSub() {
      const codes = selectedCodes();
      return Boolean(codes.major && codes.middle && codes.small && codes.sub);
    }
    function hasSelectedScope() {
      return Boolean(selectedCodes().major);
    }
    function scopeLabel() {
      const codes = selectedCodes();
      const parts = [codes.major, codes.middle, codes.small, codes.sub].filter(Boolean);
      return parts.length ? parts.join('-') : '전체 NCS';
    }
    function clearDetail(message='왼쪽에서 항목을 선택하세요.') {
      currentDetail = null;
      q('detail').style.display = 'none';
      q('detailKind').textContent = '';
      q('emptyDetail').style.display = 'block';
      q('emptyDetail').textContent = message;
    }
    function setHrScope() {
      q('majorCode').value = '02';
      q('middleCode').value = '02';
      q('smallCode').value = '02';
      q('subCode').value = '01';
      refreshAll();
    }
    function setManagementSupportMvp() {
      q('majorCode').value = '02';
      q('middleCode').value = '';
      q('smallCode').value = '';
      q('subCode').value = '';
      q('keyword').value = '경영지원';
      refreshAll();
    }
    function statusClass(status) {
      if (['human_reviewed','accepted','reviewed'].includes(status)) return 'warn';
      if (['matched','processed','defined','linked','definition:defined','relation:linked','mapped_source','training'].includes(status)) return 'ok';
      if (['api_failed','error'].includes(status)) return 'bad';
      if (['not_collected','no_data','raw','warning','missing','unlinked','definition:missing','relation:unlinked','needs_review','no_training'].includes(status)) return 'warn';
      return '';
    }
    function statusLabel(status) {
      if (['human_reviewed','accepted','reviewed'].includes(status)) return 'review-state marker';
      return status || '';
    }
    function statusPill(status) {
      const raw = status || '';
      const label = statusLabel(raw);
      const title = raw && raw !== label ? ` title="${esc(raw)}"` : '';
      return `<span class="pill ${statusClass(raw)}"${title}>${esc(label)}</span>`;
    }
    function progressBar(percent) {
      const value = Math.max(0, Math.min(100, Number(percent || 0)));
      return `<div class="progress"><span style="width:${value.toFixed(1)}%"></span></div>`;
    }
    function taxonomyParams(level) {
      const params = new URLSearchParams();
      const codes = selectedCodes();
      params.set('level', level);
      params.set('limit', level === 'major' ? '100' : '500');
      if (codes.major) params.set('major_code', codes.major);
      if (codes.middle) params.set('middle_code', codes.middle);
      if (codes.small) params.set('small_code', codes.small);
      if (codes.sub) params.set('sub_code', codes.sub);
      return params;
    }
    function renderEmpty(target, message) {
      q(target).innerHTML = `<div class="muted" style="padding:10px;">${esc(message)}</div>`;
    }
    function renderMajorTiles(nodes) {
      const codes = selectedCodes();
      q('majorTiles').innerHTML = nodes.map(node => {
        const active = node.major_code === codes.major ? ' active' : '';
        const hasStats = node.element_count !== undefined && node.element_count !== null;
        const pct = Number(node.element_percent || 0);
        const meta = hasStats
          ? `<div class="tile-meta">요소 API ${pct.toFixed(1)}% · ${fmt.format(node.element_matched)} / ${fmt.format(node.element_count)}</div>${progressBar(pct)}`
          : '<div class="tile-meta">대분류 선택</div>';
        return `<button type="button" class="major-tile${active}" data-major-code="${esc(node.major_code)}" aria-pressed="${active ? 'true' : 'false'}">
          <div class="major-icon">${majorIcons[node.major_code] || node.major_code}</div>
          <div class="tile-title">${esc(node.major_code)}. ${esc(node.name)}</div>
          ${meta}
        </button>`;
      }).join('');
    }
    function renderNodeList(target, metaTarget, nodes, level) {
      const codes = selectedCodes();
      q(metaTarget).textContent = nodes.length ? `${fmt.format(nodes.length)}개` : '';
      if (!nodes.length) {
        const messages = {middle:'대분류를 선택하세요.', small:'중분류를 선택하세요.', sub:'소분류를 선택하세요.'};
        renderEmpty(target, messages[level] || '조회 결과가 없습니다.');
        return;
      }
      q(target).innerHTML = nodes.map(node => {
        const active =
          (level === 'middle' && node.middle_code === codes.middle) ||
          (level === 'small' && node.small_code === codes.small) ||
          (level === 'sub' && node.sub_code === codes.sub);
        const pct = Number(node.element_percent || 0);
        const click =
          level === 'middle'
            ? `selectMiddle('${esc(node.major_code)}','${esc(node.middle_code)}')`
            : level === 'small'
              ? `selectSmall('${esc(node.major_code)}','${esc(node.middle_code)}','${esc(node.small_code)}')`
              : `selectSub('${esc(node.major_code)}','${esc(node.middle_code)}','${esc(node.small_code)}','${esc(node.sub_code)}')`;
        return `<button class="node${active ? ' active' : ''}" onclick="${click}">
          <span class="node-icon">${esc(node.code)}</span>
          <span class="node-main">
            <span class="node-title">${esc(node.name)}</span>
            <span class="node-sub">요소 API ${pct.toFixed(1)}% · 세분류 ${fmt.format(node.classification_count)} · 단위 ${fmt.format(node.unit_count)}</span>
            ${progressBar(pct)}
          </span>
        </button>`;
      }).join('');
    }
    function renderUnits(units) {
      q('unitMeta').textContent = units.length ? `${fmt.format(units.length)}개 표시` : '';
      if (!units.length) {
        renderEmpty('unitList', selectedCodes().sub ? '능력단위가 없습니다.' : '세분류를 선택하세요.');
        return;
      }
      q('unitList').innerHTML = units.map(unit => {
        const total = Number(unit.element_count || 0);
        const matched = Number(unit.element_matched || 0);
        const pct = total ? matched / total * 100 : 0;
        return `<button class="node" onclick="openUnit('${esc(unit.unit_code)}')">
          <span class="node-icon">${esc(String(unit.unit_level_raw || '-'))}</span>
          <span class="node-main">
            <span class="node-title">${esc(unit.unit_name_refined || unit.unit_name_raw)}</span>
            <span class="node-sub">${esc(unit.unit_code)} · 요소 API ${pct.toFixed(1)}% · ${fmt.format(matched)} / ${fmt.format(total)}</span>
            ${progressBar(pct)}
          </span>
        </button>`;
      }).join('');
    }
    async function loadTaxonomy() {
      const codes = selectedCodes();
      const majors = await api('/api/taxonomy?' + taxonomyParams('major').toString());
      const middles = codes.major ? await api('/api/taxonomy?' + taxonomyParams('middle').toString()) : {nodes:[]};
      const smalls = codes.major && codes.middle ? await api('/api/taxonomy?' + taxonomyParams('small').toString()) : {nodes:[]};
      const subs = codes.major && codes.middle && codes.small ? await api('/api/taxonomy?' + taxonomyParams('sub').toString()) : {nodes:[]};
      const units = codes.major && codes.middle && codes.small && codes.sub
        ? await api('/api/units?' + scopeParams(false).toString())
        : {units:[]};
      renderMajorTiles(majors.nodes);
      renderNodeList('middleList', 'middleMeta', middles.nodes, 'middle');
      renderNodeList('smallList', 'smallMeta', smalls.nodes, 'small');
      renderNodeList('subList', 'subMeta', subs.nodes, 'sub');
      renderUnits(units.units || []);
    }
    async function selectMajor(major) {
      setScope(major, '', '', '');
      q('keyword').value = '';
      currentCard = {kind:'classification', state:'processed', title:'선택 범위 세분류'};
      clearDetail('세분류나 작업 항목을 선택하면 상세 정제 항목을 열 수 있습니다.');
      await refreshAll();
    }
    async function selectMiddle(major, middle) {
      setScope(major, middle, '', '');
      q('keyword').value = '';
      currentCard = {kind:'classification', state:'processed', title:'선택 범위 세분류'};
      clearDetail('세분류나 작업 항목을 선택하면 상세 정제 항목을 열 수 있습니다.');
      await refreshAll();
    }
    async function selectSmall(major, middle, small) {
      setScope(major, middle, small, '');
      q('keyword').value = '';
      currentCard = {kind:'classification', state:'processed', title:'선택 범위 세분류'};
      clearDetail('세분류나 작업 항목을 선택하면 상세 정제 항목을 열 수 있습니다.');
      await refreshAll();
    }
    async function selectSub(major, middle, small, sub) {
      setScope(major, middle, small, sub);
      q('keyword').value = '';
      currentCard = {kind:'criteria', state:'raw', title:'수행준거 미정제'};
      clearDetail();
      await refreshAll();
    }
    async function openUnit(unitCode) {
      q('keyword').value = unitCode;
      currentCard = {kind:'element', state:'processed', title:'능력단위요소 전처리 완료'};
      await loadCurrentItems();
      await loadDetail('unit', unitCode);
    }
    function selectedName(target) {
      const el = q(target).querySelector('.node.active .node-title');
      return el ? el.textContent : '';
    }
    function selectedMajorName() {
      const el = q('majorTiles').querySelector('.major-tile.active .tile-title');
      return el ? el.textContent : '';
    }
    function renderSubStatus(phases) {
      const codes = selectedCodes();
      const box = q('subStatus');
      if (!codes.major) {
        box.classList.remove('visible');
        box.innerHTML = '';
        return;
      }
      const path = [
        selectedMajorName(),
        selectedName('middleList'),
        selectedName('smallList'),
        selectedName('subList')
      ].filter(Boolean).join(' > ');
      box.classList.add('visible');
      box.innerHTML = `<h3>${esc(scopeLabel())} 선택 범위 온톨로지 준비 현황</h3>
        <div class="muted" style="margin-bottom:10px;">${esc(path)}</div>
        <div class="status-grid">
          ${phases.map(phase => `<div class="status-cell">
            <b>${esc(phase.name)}</b>
            ${statusPill(phase.status)}
            <div class="tile-meta">${phase.percent.toFixed(1)}% · 남은 작업 ${fmt.format(phase.remaining)}</div>
            ${progressBar(phase.percent)}
          </div>`).join('')}
        </div>
        <div class="quick-actions">
          <button class="link" onclick="selectCard('classification','processed','선택 범위 세분류')">세분류/직무정의</button>
          <button class="link" onclick="selectCard('unit','processed','능력단위 전처리 완료')">능력단위</button>
          <button class="link" onclick="selectCard('element','processed','능력단위요소 전처리 완료')">능력단위요소</button>
          <button class="link" onclick="selectCard('criteria','raw','수행준거 미정제')">수행준거 정제</button>
          <button class="link" onclick="selectCard('ksa','raw','KSA 미정제')">KSA 정제</button>
          <button class="link" onclick="selectCard('element','api_not_collected','요소 API 미수집')">요소 API 미수집</button>
          <button class="link" onclick="selectCard('element','api_problem','요소 API 실패/없음')">요소 API 실패</button>
          <button class="link" onclick="selectCard('quality','open','열린 품질 이슈')">품질 이슈</button>
        </div>`;
    }

    async function refreshAll() {
      await loadTaxonomy();
      await loadStatus();
      await loadProgress();
      await loadWorkbench();
      await loadCurrentItems();
      await loadIssues();
      await loadOntologyStatus();
      await loadOntology();
      await loadRecommendationRuns();
    }

    const conceptTypeLabels = {knowledge:'지식', skill:'기술', attitude:'태도'};
    const conceptStateLabels = {
      definition_missing:'정의 미작성',
      relation_missing:'관계 미연결',
      duplicates:'중복 후보',
      reviewed:'검토 상태 보유'
    };

    async function loadOntologyStatus() {
      if (!hasSelectedScope()) {
        q('ontologyWorkbenchMeta').textContent = '';
        q('ontologyWorkbench').innerHTML = '<div class="muted">대분류를 선택하면 온톨로지 구축 현황이 표시됩니다.</div>';
        return;
      }
      const data = await api('/api/ontology-status?' + scopeParams(false).toString());
      q('ontologyWorkbenchMeta').textContent = `${scopeLabel()} 기준`;
      q('ontologyWorkbench').innerHTML = data.statuses.map(item => `<div class="ontology-stat">
        <h3>${esc(item.label)} (${esc(item.concept_type)})</h3>
        <div>전체 개념 <b>${fmt.format(item.total)}</b></div>
        <div>정의 작성 <b>${fmt.format(item.definition_done)}</b> / ${fmt.format(item.total)}</div>
        <div>관계 연결 <b>${fmt.format(item.relation_done)}</b> / ${fmt.format(item.total)}</div>
        <div>검토 상태 보유 <b>${fmt.format(item.reviewed)}</b></div>
        <button class="link" onclick="loadConceptWorkItems('${esc(item.concept_type)}','definition_missing')">정의 미작성 ${fmt.format(item.definition_missing)}</button>
        <button class="link" onclick="loadConceptWorkItems('${esc(item.concept_type)}','relation_missing')">관계 미연결 ${fmt.format(item.relation_missing)}</button>
        <button class="link" onclick="loadConceptWorkItems('${esc(item.concept_type)}','duplicates')">중복 후보 ${fmt.format(item.duplicate_like)}</button>
        <button class="link" onclick="loadConceptWorkItems('${esc(item.concept_type)}','reviewed')">검토 상태 보유 ${fmt.format(item.reviewed)}</button>
      </div>`).join('');
    }

    async function loadConceptWorkItems(conceptType, state) {
      const params = scopeParams(false);
      params.set('concept_type', conceptType);
      params.set('state', state);
      params.set('limit', '100');
      const data = await api('/api/concepts?' + params.toString());
      q('conceptWorkItems').innerHTML = data.concepts.map(item => `<tr>
        <td><b>${esc(item.concept_name)}</b><br><span class="muted">concept_id ${item.concept_id}</span></td>
        <td>${esc(conceptTypeLabels[item.concept_type] || item.concept_type)}</td>
        <td>${item.definition ? '<span class="ok">작성됨</span>' : '<span class="warn">미작성</span>'}<br><span class="muted">${esc((item.definition || '').slice(0, 80))}</span></td>
        <td>${item.relation_count ? '<span class="ok">연결됨</span>' : '<span class="warn">미연결</span>'}<br><span class="muted">${fmt.format(item.relation_count)}개</span></td>
        <td>${fmt.format(item.alias_count)}</td>
        <td>${item.sample_ksa_id ? `<button class="link" onclick="loadDetail('ksa','${esc(item.sample_ksa_id)}')">개념 정의/관계</button>` : '<span class="muted">KSA 연결 없음</span>'}</td>
      </tr>`).join('');
      if (!data.concepts.length) {
        q('conceptWorkItems').innerHTML = `<tr class="guide-row"><td colspan="6">${esc(conceptTypeLabels[conceptType] || conceptType)} · ${esc(conceptStateLabels[state] || state)} 대상이 없습니다.</td></tr>`;
      }
    }

    function renderOntologyItem(kind, id, label, text, status) {
      return `<button class="ontology-item" onclick="loadDetail('${esc(kind)}','${esc(id)}')">
        <b>${esc(label)}</b>
        <span class="muted">${esc(text || '')}</span>
        ${statusPill(status || '')}
      </button>`;
    }

    async function loadOntology() {
      if (!hasSelectedScope()) {
        q('ontologyMeta').textContent = '';
        q('ontologyTree').innerHTML = '<div class="muted">대분류를 선택하면 능력단위-요소-수행준거-KSA 구조가 표시됩니다.</div>';
        return;
      }
      const params = scopeParams(false);
      params.set('limit', hasSelectedSub() ? '50' : '12');
      const data = await api('/api/ontology?' + params.toString());
      q('ontologyMeta').textContent = `${scopeLabel()} · 능력단위 ${fmt.format(data.units.length)} / ${fmt.format(data.total_units)} 표시`;
      if (!data.units.length) {
        q('ontologyTree').innerHTML = '<div class="muted">선택 범위에 능력단위가 없습니다.</div>';
        return;
      }
      q('ontologyTree').innerHTML = data.units.map((unit, idx) => `<details class="ontology-unit" ${idx === 0 ? 'open' : ''}>
        <summary>${esc(unit.unit_code)} ${esc(unit.unit_name)} ${statusPill(unit.api_match_status)}</summary>
        <div class="ontology-body">
          ${unit.elements.map(element => `<div class="ontology-element">
            <h4>${esc(element.element_no)}. ${esc(element.element_name)} ${statusPill(element.api_match_status)}</h4>
            <div class="ontology-columns">
              <div class="ontology-group">
                <h5>수행준거</h5>
                <div class="ontology-list">
                  ${element.criteria.length ? element.criteria.map(item =>
                    renderOntologyItem('criteria', item.criteria_id, `수행준거 ${item.criteria_no}`, item.criteria_text_refined || item.criteria_text_raw, item.review_status)
                  ).join('') : '<div class="muted">수행준거 없음</div>'}
                </div>
              </div>
              <div class="ontology-group">
                <h5>KSA 지식·기술·태도</h5>
                <div class="ontology-list">
                  ${['지식','기술','태도'].map(group => {
                    const rows = element.ksa_groups[group] || [];
                    return `<div>
                      <b>${esc(group)}</b>
                      ${rows.length ? rows.map(item =>
                        renderOntologyItem(
                          'ksa',
                          item.ksa_id,
                          `${item.ksa_type_name} ${item.ksa_no} · ${item.concept_name || item.ksa_text_refined || item.ksa_text_raw}`,
                          item.definition ? `정의: ${item.definition}` : `원문: ${item.ksa_text_raw}`,
                          item.definition_status || item.review_status
                        )
                      ).join('') : '<div class="muted">항목 없음</div>'}
                    </div>`;
                  }).join('')}
                </div>
              </div>
            </div>
          </div>`).join('')}
        </div>
      </details>`).join('');
    }

    async function loadRecommendationRuns() {
      const params = new URLSearchParams();
      params.set('limit', '25');
      const keyword = q('keyword').value.trim();
      if (keyword) params.set('query', keyword);
      const data = await api('/api/recommendation-runs?' + params.toString());
      q('recommendationMeta').textContent = `${fmt.format(data.total)} runs`;
      if (!data.runs.length) {
        q('recommendationRuns').innerHTML = '<tr class="guide-row"><td colspan="5">No saved recommendation runs.</td></tr>';
        q('recommendationDetail').textContent = 'Run recommend_education_for_duty from MCP to create an audit trail.';
        return;
      }
      q('recommendationRuns').innerHTML = data.runs.map(run => {
        const summary = run.summary || {};
        const target = run.target || {};
        return `<tr>
          <td><b>${run.run_id}</b><br><span class="muted">${esc(run.created_at)}</span></td>
          <td>${esc(run.query)}</td>
          <td>${esc(target.duty_name || '')}<br><span class="muted">${esc(target.sqf_job || target.source_key || '')}</span></td>
          <td>modules ${fmt.format(summary.recommended_modules_count || 0)}<br><span class="muted">concepts ${fmt.format(summary.ontology_concepts_used || 0)}</span></td>
          <td><button class="link" onclick="loadRecommendationDetail(${run.run_id})">Evidence</button></td>
        </tr>`;
      }).join('');
      await loadRecommendationDetail(data.runs[0].run_id);
    }

    async function loadRecommendationDetail(runId) {
      const data = await api('/api/recommendation-detail?run_id=' + encodeURIComponent(runId));
      if (data.error) {
        q('recommendationDetail').textContent = data.error;
        return;
      }
      const items = (data.items || []).map(item => {
        const payload = item.payload || {};
        return `#${item.rank} ${payload.learn_module_name || item.learn_module_name || 'NCS-derived objective'} (${item.confidence_grade}, ${Number(item.confidence_score || 0).toFixed(2)})`;
      }).join('\\n');
      const evidence = (data.evidence || []).slice(0, 30).map(ev => {
        return `- ${ev.evidence_type} ${ev.source_table || ''} ${ev.source_id || ''}: ${ev.evidence_summary || ev.evidence_text || ''}`;
      }).join('\\n');
      q('recommendationDetail').textContent = [
        `Run ${data.run.run_id} / ${data.run.created_at}`,
        `Query: ${data.run.query}`,
        '',
        'Items:',
        items || '(none)',
        '',
        'Evidence:',
        evidence || '(none)'
      ].join('\\n');
    }

    async function refreshOverview() {
      await loadStatus();
      await loadProgress();
      await loadWorkbench();
      await loadIssues();
    }

    function scheduleAutoRefresh() {
      if (overviewTimer) clearInterval(overviewTimer);
      overviewTimer = setInterval(() => {
        refreshOverview().catch(err => {
          q('liveStatus').textContent = `자동갱신 오류: ${err.message}`;
        });
      }, 30000);
    }

    async function loadStatus() {
      const data = await api('/api/status');
      const loadedAt = new Date().toLocaleTimeString('ko-KR');
      q('stamp').textContent = `DB ${data.generated_at} / 화면 ${loadedAt}`;
      q('liveStatus').textContent = `자동갱신 30초 / 마지막 ${loadedAt}`;
      const cp = data.counts;
      const ep = data.element_progress;
      const sqf = data.sqf;
      const onto = data.ontology;
      q('summary').innerHTML = [
        `<div class="panel"><span class="muted">능력단위</span><strong>${fmt.format(cp.competency_units)}</strong><span class="ok">API matched ${fmt.format(data.unit_api_status.matched || 0)}</span></div>`,
        `<div class="panel"><span class="muted">능력단위요소 API 검증</span><strong>${ep.percent.toFixed(1)}%</strong><span class="muted">${fmt.format(ep.matched)} / ${fmt.format(ep.total)}</span></div>`,
        `<div class="panel"><span class="muted">SQF 직무수준</span><strong>${fmt.format(cp.sqf_duties || 0)}</strong><span class="muted">제공 대분류 ${fmt.format(sqf.major_codes_with_data || 0)}개</span></div>`,
        `<div class="panel"><span class="muted">경영지원 MVP</span><strong>${fmt.format(sqf.management_support_duties || 0)}</strong><span class="${sqf.management_support_duties ? 'ok' : 'warn'}">SQF 경영지원 직무</span></div>`,
        `<div class="panel"><span class="muted">NCS-SQF 매핑</span><strong>${fmt.format(onto.matches || 0)}</strong><span class="${onto.match_table_present ? 'warn' : 'bad'}">${onto.match_table_present ? '후보 검토 필요' : '테이블 생성 필요'}</span></div>`,
        `<div class="panel"><span class="muted">열린 품질 이슈</span><strong>${fmt.format(data.quality.open_issues)}</strong><span class="${data.quality.actionable_issues ? 'warn' : 'ok'}">actionable ${fmt.format(data.quality.actionable_issues || 0)}</span><span class="muted">info ${fmt.format(data.quality.info_issues || 0)} / resolved ${fmt.format(data.quality.resolved_issues)}</span></div>`
      ].join('');
      const issueTypes = [''].concat(data.issue_types);
      q('issueType').innerHTML = issueTypes.map(v => `<option value="${esc(v)}">${esc(v || '전체 이슈')}</option>`).join('');
    }

    async function loadProgress() {
      const data = await api('/api/progress?' + scopeParams(false).toString());
      q('phases').innerHTML = data.phases.map(phase => `<tr>
        <td><b>${esc(phase.name)}</b><br>${statusPill(phase.status)}</td>
        <td>${esc(phase.meaning)}</td>
        <td>${fmt.format(phase.completed)} / ${fmt.format(phase.total)}<br><span class="muted">${phase.percent.toFixed(1)}%</span></td>
        <td class="${phase.remaining ? 'warn' : 'ok'}">${fmt.format(phase.remaining)}<br><span class="muted">${esc(phase.remaining_detail || '')}</span></td>
        <td>${esc(phase.method)}</td>
        <td><button class="link" onclick="selectCard('${esc(phase.kind)}','${esc(phase.state)}','${esc(phase.title)}')">내역 보기</button></td>
      </tr>`).join('');
      renderSubStatus(data.phases);
    }

    async function loadWorkbench() {
      const data = await api('/api/workbench?' + scopeParams(false).toString());
      q('cards').innerHTML = data.cards.map(card => {
        const active = card.kind === currentCard.kind && card.state === currentCard.state ? ' active' : '';
        return `<div class="card${active}" onclick="selectCard('${esc(card.kind)}','${esc(card.state)}','${esc(card.title)}')">
          <div class="label">${esc(card.group)}</div>
          <div class="value">${fmt.format(card.count)}</div>
          <div><b>${esc(card.title)}</b></div>
          <div class="sub">${esc(card.description)}</div>
        </div>`;
      }).join('');
    }

    async function selectCard(kind, state, title) {
      currentCard = {kind, state, title};
      await loadWorkbench();
      await loadCurrentItems();
    }

    async function loadCurrentItems() {
      if (!hasSelectedScope() && currentCard.kind !== 'quality') {
        q('listTitle').textContent = '분류 선택 후 전처리 항목 표시';
        q('listMeta').textContent = '대분류를 먼저 선택하세요.';
        q('items').innerHTML = '<tr class="guide-row"><td colspan="5">대분류를 클릭하면 이 영역에 선택 범위의 세분류와 전처리 항목이 표시됩니다.</td></tr>';
        clearDetail('분류와 작업 항목을 선택하면 상세 정제 항목을 열 수 있습니다.');
        return;
      }
      const params = scopeParams(true);
      params.set('kind', currentCard.kind);
      params.set('state', currentCard.state);
      const data = await api('/api/items?' + params.toString());
      q('listTitle').textContent = `${currentCard.title} · ${scopeLabel()}`;
      q('listMeta').textContent = `${data.total}건 중 ${data.items.length}건 표시`;
      q('items').innerHTML = data.items.map(item => `<tr>
        <td>${statusPill(item.status)}<br>${item.api_status ? statusPill(item.api_status) : ''}</td>
        <td><b>${esc(item.id)}</b><br><span class="muted">${esc(item.code || '')}</span></td>
        <td>${esc(item.context || '')}</td>
        <td><b>${esc(item.title || '')}</b><br><span class="muted">${esc(item.body || '').slice(0, 260)}</span></td>
        <td><button class="link" onclick="loadDetail('${esc(item.kind)}','${esc(item.id)}')">상세/정제</button></td>
      </tr>`).join('');
      if (!data.items.length) {
        q('items').innerHTML = '<tr><td colspan="5" class="muted">조회 결과가 없습니다.</td></tr>';
      }
    }

    async function loadDetail(kind, id) {
      const data = await api('/api/item-detail?kind=' + encodeURIComponent(kind) + '&id=' + encodeURIComponent(id));
      currentDetail = data.item;
      q('emptyDetail').style.display = 'none';
      q('detail').style.display = 'block';
      q('detailKind').textContent = `${currentDetail.kind} / ${currentDetail.id}`;
      const labels = {
        classification: ['세분류명', '세분류명', '직무정의 원문', '직무정의 정제본'],
        unit: ['능력단위명 원문', '능력단위명 정제본', '능력단위 정의 원문', '능력단위 정의 정제본'],
        element: ['능력단위요소명 원문', '능력단위요소명 정제본', 'API 요소명', '요소 설명 정제본'],
        criteria: ['수행준거 번호', '수행준거 번호', '수행준거 원문', '수행준거 정제본'],
        ksa: ['KSA 유형/번호', '대표 개념명', 'KSA 원문', '개념 정의'],
        quality: ['이슈 유형', '이슈 유형', '이슈 내용', '권장 조치']
      }[currentDetail.kind] || ['원문 명칭', '정제 명칭', '원문/정의/내용', '정제 내용'];
      q('titleRawLabel').textContent = labels[0];
      q('titleRefinedLabel').textContent = labels[1];
      q('bodyRawLabel').textContent = labels[2];
      q('bodyRefinedLabel').textContent = labels[3];
      q('detailStatus').innerHTML = [
        statusPill(currentDetail.status || ''),
        currentDetail.api_status ? statusPill(currentDetail.api_status) : '',
        currentDetail.definition_status ? statusPill(`definition:${currentDetail.definition_status}`) : '',
        currentDetail.relation_status ? statusPill(`relation:${currentDetail.relation_status}`) : '',
        currentDetail.body_refined ? '<span class="ok">정의 작성됨</span>' : '<span class="warn">정의 없음</span>'
      ].filter(Boolean).join(' ');
      q('context').textContent = currentDetail.context || '';
      q('titleRaw').textContent = currentDetail.title_raw || '';
      q('bodyRaw').textContent = currentDetail.body_raw || '';
      q('titleRefined').value = currentDetail.title_refined || '';
      q('titleRefined').placeholder = currentDetail.title_raw || '';
      q('bodyRefined').value = currentDetail.body_refined || '';
      q('bodyRefined').placeholder = currentDetail.body_raw || '';
      q('titleEditWrap').style.display = currentDetail.can_refine_title ? 'block' : 'none';
      q('bodyEditWrap').style.display = currentDetail.can_refine_body ? 'block' : 'none';
      q('ontologyConceptFields').style.display = currentDetail.kind === 'ksa' ? 'block' : 'none';
      if (currentDetail.kind === 'ksa') {
        q('conceptAliases').value = (currentDetail.aliases || []).join('\\n');
        q('parentConcepts').value = ((currentDetail.relations || {}).parent || []).join('\\n');
        q('childConcepts').value = ((currentDetail.relations || {}).child || []).join('\\n');
        q('relatedConcepts').value = ((currentDetail.relations || {}).related || []).join('\\n');
        q('relatedCriteria').innerHTML = (currentDetail.related_criteria || []).length
          ? currentDetail.related_criteria.map(item => `<div><b>수행준거 ${esc(item.criteria_no)}</b><br>${esc(item.criteria_text_raw)}</div>`).join('')
          : '<span class="muted">연결된 수행준거가 없습니다.</span>';
      }
    }

    async function saveCurrentDetail() {
      if (!currentDetail) return;
      if (!currentDetail.can_refine_title && !currentDetail.can_refine_body) {
        alert('이 항목은 읽기 전용 근거입니다.');
        return;
      }
      const needsTitle = currentDetail.can_refine_title && !q('titleRefined').value.trim();
      const needsBody = currentDetail.can_refine_body && !q('bodyRefined').value.trim();
      if (needsTitle || needsBody) {
        alert('정제본이 비어 있습니다. 직접 입력하거나 "원문 그대로 사용"을 누른 뒤 저장하세요.');
        return;
      }
      await api('/api/preprocess', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          kind: currentDetail.kind,
          id: currentDetail.id,
          title_refined: q('titleRefined').value,
          body_refined: q('bodyRefined').value,
          aliases: q('conceptAliases').value,
          parent_concepts: q('parentConcepts').value,
          child_concepts: q('childConcepts').value,
          related_concepts: q('relatedConcepts').value,
          reviewer_id: q('manualReviewerId').value,
          source_decision_packet: q('manualSourcePacket').value,
          source_artifact_hash: q('manualSourceHash').value,
          rationale: q('manualRationale').value,
          evidence_refs: q('manualEvidenceRefs').value
        })
      });
      await loadDetail(currentDetail.kind, currentDetail.id);
      await loadCurrentItems();
      await loadStatus();
      await loadWorkbench();
    }

    function fillRawAsRefined() {
      if (!currentDetail) return;
      if (currentDetail.kind === 'ksa') {
        if (currentDetail.can_refine_title && !q('titleRefined').value.trim()) {
          q('titleRefined').value = currentDetail.body_raw || currentDetail.title_raw || '';
        }
        alert('KSA 개념 정의는 원문을 그대로 복사하지 않습니다. 정의는 직접 작성하세요.');
        return;
      }
      if (currentDetail.can_refine_title && !q('titleRefined').value.trim()) {
        q('titleRefined').value = currentDetail.title_raw || '';
      }
      if (currentDetail.can_refine_body && !q('bodyRefined').value.trim()) {
        q('bodyRefined').value = currentDetail.body_raw || '';
      }
    }

    async function loadIssues() {
      if (!hasSelectedScope()) {
        q('issues').innerHTML = '<tr class="guide-row"><td colspan="6">대분류를 선택하면 이 영역에 선택 범위의 품질 이슈가 표시됩니다.</td></tr>';
        return;
      }
      const params = scopeParams(false);
      params.set('limit', '100');
      if (q('targetType').value) params.set('target_type', q('targetType').value);
      if (q('issueType').value) params.set('issue_type', q('issueType').value);
      const data = await api('/api/issues?' + params.toString());
      q('issues').innerHTML = data.issues.map(item => `<tr>
        <td>${item.issue_id}</td>
        <td>${esc(item.issue_type)}</td>
        <td>${esc(item.target_type)}<br>${esc(item.target_id)}</td>
        <td class="${statusClass(item.severity)}">${esc(item.severity)}</td>
        <td><b>${esc(item.unit_code || '')}</b> ${esc(item.unit_name || '')}<br>${esc(item.raw_text || item.issue_detail || '')}</td>
        <td><button class="link" onclick="loadDetail('${esc(item.target_type)}','${esc(item.target_id)}')">상세/정제</button><br><button class="secondary" onclick="resolveIssue(${item.issue_id})">해결 처리</button></td>
      </tr>`).join('');
      if (!data.issues.length) q('issues').innerHTML = '<tr><td colspan="6" class="muted">열린 이슈가 없습니다.</td></tr>';
    }

    async function resolveIssue(issueId) {
      await api('/api/resolve', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({issue_id: issueId})
      });
      await loadIssues();
      await loadStatus();
      await loadWorkbench();
    }

    q('majorTiles').addEventListener('click', (event) => {
      const button = event.target.closest('[data-major-code]');
      if (!button) return;
      selectMajor(button.dataset.majorCode).catch(err => alert(err.message));
    });
    renderMajorTiles(fallbackMajorNodes);
    refreshAll().then(scheduleAutoRefresh).catch(err => alert(err.message));
  </script>
</body>
</html>
"""


def _public_review_status_display(row: dict) -> str:
    status = str(row.get("review_status_display") or "").strip()
    if status.startswith("legacy_status_needs_reconfirmation:"):
        return "legacy_status_needs_reconfirmation"
    if status.lower() in {"human_reviewed", "reviewed", "accepted", "trusted"}:
        return "status_suppressed_pending_reconfirmation"
    return status or "pending_reconfirmation"


def render_aihr_provenance_reconfirmation_html(payload: dict, source_path: Path) -> str:
    payload = public_aihr_provenance_reconfirmation_payload(payload)
    rows = [row for row in payload.get("rows") or [] if isinstance(row, dict)]
    source_summary = (
        payload.get("source_audit_summary")
        if isinstance(payload.get("source_audit_summary"), dict)
        else {}
    )
    display_counts: dict[str, int] = {}
    for row in rows:
        status_display = _public_review_status_display(row)
        display_counts[status_display] = display_counts.get(status_display, 0) + 1
    row_parts = []
    for row in rows:
        status_display = _public_review_status_display(row)
        row_parts.append(
            "<tr>"
            f"<td>{_html_escape(row.get('order'))}</td>"
            f"<td>{_html_escape(row.get('surface'))}<br><span class=\"muted\">{_html_escape(row.get('target_table'))}:{_html_escape(row.get('target_id'))}</span></td>"
            f"<td>{_html_escape(status_display)}<br><span class=\"muted\">raw status suppressed</span></td>"
            f"<td>{_html_escape(row.get('status_trust'))}</td>"
            f"<td>{_html_escape(row.get('provenance_state'))}</td>"
            f"<td>packet={_html_escape(row.get('source_decision_packet_available'))}<br>rationale={_html_escape(row.get('rationale_available'))}<br>evidence_refs={_html_escape(row.get('evidence_refs_available'))}</td>"
            f"<td>{_html_escape(row.get('display'))}</td>"
            f"<td>{_html_escape(row.get('requested_decision'))}</td>"
            "</tr>"
        )
    row_html = "".join(row_parts)
    count_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(status)}</td>"
        f"<td>{_html_escape(count)}</td>"
        "</tr>"
        for status, count in sorted(display_counts.items())
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI-HR Provenance Reconfirmation</title>
  <style>
    :root {{ --ink:#172033; --muted:#667085; --line:#d9e0ea; --bg:#f6f8fb; --panel:#fff; --ok:#047857; --warn:#b45309; --bad:#b91c1c; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Segoe UI, Arial, "Malgun Gothic", sans-serif; background:var(--bg); color:var(--ink); }}
    main {{ max-width:1400px; margin:0 auto; padding:24px; }}
    h1 {{ margin:0 0 6px; font-size:26px; }}
    h2 {{ margin:22px 0 8px; font-size:18px; }}
    .muted {{ color:var(--muted); }}
    .guardrail {{ background:#fff8c5; border:1px solid #d0a000; border-radius:8px; padding:10px 12px; margin:14px 0; }}
    .summary {{ display:grid; grid-template-columns:repeat(5, minmax(0,1fr)); gap:10px; margin-top:16px; }}
    .metric {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }}
    .metric span {{ display:block; color:var(--muted); font-size:12px; }}
    .metric strong {{ display:block; margin-top:4px; font-size:22px; }}
    .ok {{ color:var(--ok); }} .warn {{ color:var(--warn); }} .bad {{ color:var(--bad); }}
    table {{ width:100%; border-collapse:collapse; background:var(--panel); table-layout:fixed; }}
    th, td {{ border:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; overflow-wrap:anywhere; font-size:13px; }}
    th {{ background:#eef2f5; }}
    @media (max-width:760px) {{ main {{ padding:12px; }} .summary {{ grid-template-columns:1fr; }} table {{ display:block; overflow-x:auto; }} th, td {{ min-width:180px; }} }}
  </style>
</head>
<body>
<main>
  <p class="muted">{_html_escape(source_path.name)}</p>
  <h1>AI-HR Provenance Reconfirmation</h1>
  <div class="guardrail">Read-only Human Review surface. No DB writes, no approval claim, no status update. Legacy trusted/reviewed raw labels are suppressed and require packet-backed reconfirmation before future trusted import.</div>
  <section class="summary">
    <div class="metric"><span>ok</span><strong class="{ 'ok' if payload.get('ok') else 'bad' }">{_html_escape(payload.get('ok'))}</strong></div>
    <div class="metric"><span>rows</span><strong>{_html_escape(payload.get('row_count'))}</strong></div>
    <div class="metric"><span>legacy needs reconfirmation</span><strong class="{ 'warn' if payload.get('legacy_status_needs_reconfirmation_count') else 'ok' }">{_html_escape(payload.get('legacy_status_needs_reconfirmation_count'))}</strong></div>
    <div class="metric"><span>packet-backed source rows</span><strong>{_html_escape(source_summary.get('rows_packet_backed'))}</strong></div>
    <div class="metric"><span>status updates allowed</span><strong class="{ 'bad' if payload.get('status_update_allowed') else 'ok' }">{_html_escape(payload.get('status_update_allowed'))}</strong></div>
  </section>
  <h2>Status Display Counts</h2>
  <table><thead><tr><th>Status display</th><th>Count</th></tr></thead><tbody>{count_rows or '<tr><td colspan="2">No status display counts.</td></tr>'}</tbody></table>
  <h2>Rows</h2>
  <table><thead><tr><th>Order</th><th>Surface / Target</th><th>Status Display</th><th>Status Trust</th><th>Provenance</th><th>Packet Fields</th><th>Display</th><th>Requested Decision</th></tr></thead><tbody>{row_html or '<tr><td colspan="8">No reconfirmation rows.</td></tr>'}</tbody></table>
</main>
</body>
</html>
"""


def public_aihr_provenance_reconfirmation_payload(payload: dict) -> dict:
    public_payload = copy.deepcopy(payload)
    public_payload.pop("db_path", None)
    public_payload.pop("local_db_path", None)
    public_payload.pop("database_path", None)
    public_payload.setdefault("source_database_ref", "configured_ncs_database")
    public_rows = []
    for row in public_payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        row.pop("current_review_status", None)
        row.pop("raw_review_status", None)
        row["review_status_display"] = _public_review_status_display(row)
        row["raw_review_status_suppressed"] = True
        public_rows.append(row)
    public_payload["rows"] = public_rows
    public_payload["review_status_display_counts"] = {
        row["review_status_display"]: sum(
            1 for candidate in public_rows if candidate.get("review_status_display") == row["review_status_display"]
        )
        for row in public_rows
    }
    public_payload["public_api_contract"] = {
        "raw_review_status_suppressed": True,
        "use_fields": ["review_status_display", "status_trust", "status_disposition"],
        "trusted_status_claim_allowed": False,
    }
    return public_payload


def _review_seedpack_context_pairs(context: dict) -> list[tuple[str, object]]:
    preferred = (
        "concept_name",
        "concept_type",
        "concept_definition_status",
        "compe_unit_name",
        "ncs_cl_cd",
        "element_name",
        "criteria_text_raw",
        "ksa_text_raw",
        "link_method",
        "confidence_score",
        "train_goal",
        "meth_name",
        "train_time",
    )
    pairs: list[tuple[str, object]] = []
    for key in preferred:
        value = context.get(key)
        if value not in (None, "", [], {}):
            pairs.append((key, _public_review_seedpack_text(value)))
    return pairs


_REVIEW_SEEDPACK_PUBLIC_BATCH_KEYS = {
    "record_type",
    "format_version",
    "seedpack_id",
    "item_count",
    "created_at",
    "generated_at",
}
_REVIEW_SEEDPACK_ALLOWED_DECISIONS = {"approve", "reject", "defer"}
_REVIEW_SEEDPACK_RAW_STATUS_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:human_reviewed|reviewed|accepted|trusted)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_REVIEW_SEEDPACK_SOURCE_PAYLOAD_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])source_payload(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def _public_review_seedpack_text(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = _public_aihr_command_text(text)
    text = _REVIEW_SEEDPACK_RAW_STATUS_TOKEN_RE.sub(
        "status_suppressed_pending_reconfirmation",
        text,
    )
    return _REVIEW_SEEDPACK_SOURCE_PAYLOAD_TOKEN_RE.sub("internal_payload_suppressed", text)


def _public_review_seedpack_allowed_decisions(batch: dict | None) -> list[str]:
    decisions = []
    if isinstance(batch, dict):
        for value in batch.get("allowed_decisions") or []:
            decision = str(value or "").strip()
            if decision in _REVIEW_SEEDPACK_ALLOWED_DECISIONS:
                decisions.append(decision)
    return decisions or ["approve", "reject", "defer"]


def _public_review_seedpack_batch(batch: dict | None) -> dict:
    if not isinstance(batch, dict):
        return {}
    public_batch: dict[str, object] = {}
    for key in _REVIEW_SEEDPACK_PUBLIC_BATCH_KEYS:
        value = batch.get(key)
        if value in (None, "", [], {}):
            continue
        public_batch[key] = _public_review_seedpack_text(value)
    public_batch["allowed_decisions"] = _public_review_seedpack_allowed_decisions(batch)
    public_batch["public_metadata_only"] = True
    if any(key not in _REVIEW_SEEDPACK_PUBLIC_BATCH_KEYS | {"allowed_decisions"} for key in batch):
        public_batch["private_metadata_suppressed"] = True
    return public_batch


def load_review_seedpack_payload(path: Path, *, item_limit: int = 1000) -> dict:
    batch: dict | None = None
    items: list[dict] = []
    parse_errors: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                parse_errors.append({"line": line_number, "error": str(exc)})
                continue
            if not isinstance(record, dict):
                parse_errors.append({"line": line_number, "error": "record_not_object"})
                continue
            record_type = str(record.get("record_type") or "")
            if record_type == "batch" and batch is None:
                batch = record
                continue
            if record_type in {"review_item", "ontology_transferability_review_item"} or record.get("issue_type"):
                if len(items) < item_limit:
                    context = record.get("context") if isinstance(record.get("context"), dict) else {}
                    issue = record.get("issue") if isinstance(record.get("issue"), dict) else {}
                    source_decision_metadata_present = any(
                        str(record.get(field) or "").strip()
                        for field in ("decision", "reviewer_id", "reviewed_at", "rationale")
                    )
                    items.append(
                        {
                            "sequence": _public_review_seedpack_text(
                                record.get("sequence") or len(items) + 1
                            ),
                            "issue_type": _public_review_seedpack_text(
                                record.get("issue_type") or issue.get("issue_type")
                            ),
                            "target_type": _public_review_seedpack_text(
                                record.get("target_type") or issue.get("target_type")
                            ),
                            "target_id": _public_review_seedpack_text(
                                record.get("target_id") or issue.get("target_id")
                            ),
                            "priority_score": record.get("priority_score"),
                            "priority_reason": _public_review_seedpack_text(
                                record.get("priority_reason")
                            ),
                            "review_gate_status": "pending_human_decision",
                            "raw_review_status_suppressed": True,
                            "suggested_action": _public_review_seedpack_text(
                                record.get("suggested_action") or issue.get("suggested_action")
                            ),
                            "issue_detail": _public_review_seedpack_text(
                                record.get("issue_detail") or issue.get("issue_detail")
                            ),
                            "source_context_excerpt": _public_review_seedpack_text(
                                record.get("source_context_excerpt")
                            ),
                            "target_snapshot_hash": _public_review_seedpack_text(
                                record.get("target_snapshot_hash")
                            ),
                            "decision": "",
                            "reviewer_id": "",
                            "reviewed_at": "",
                            "rationale": "",
                            "decision_metadata_suppressed": source_decision_metadata_present,
                            "context_pairs": _review_seedpack_context_pairs(context),
                        }
                    )
    issue_type_counts: dict[str, int] = {}
    target_type_counts: dict[str, int] = {}
    for item in items:
        issue_type = str(item.get("issue_type") or "unknown")
        target_type = str(item.get("target_type") or "unknown")
        issue_type_counts[issue_type] = issue_type_counts.get(issue_type, 0) + 1
        target_type_counts[target_type] = target_type_counts.get(target_type, 0) + 1
    decision_metadata_suppressed_count = sum(
        1 for item in items if item.get("decision_metadata_suppressed")
    )
    return {
        "schema": "ontology_review_board_seedpack_v1",
        "source_path": _public_aihr_path_text(path),
        "batch": _public_review_seedpack_batch(batch),
        "item_count": len(items),
        "item_limit": item_limit,
        "items": items,
        "issue_type_counts": issue_type_counts,
        "target_type_counts": target_type_counts,
        "allowed_decisions": _public_review_seedpack_allowed_decisions(batch),
        "db_writes": False,
        "status_update_allowed": False,
        "approval_claim": False,
        "decision_metadata_suppressed_count": decision_metadata_suppressed_count,
        "public_api_contract": {
            "raw_review_status_suppressed": True,
            "decision_metadata_suppressed": True,
            "operator_decision_metadata_public": False,
            "trusted_status_claim_allowed": False,
            "use_fields": ["review_gate_status"],
        },
        "parse_errors": parse_errors[:20],
    }


def render_ontology_review_board_html(payload: dict, source_path: Path) -> str:
    payload = public_aihr_dashboard_payload(payload)
    items = payload.get("items") or []
    issue_count_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(key)}</td>"
        f"<td>{_html_escape(value)}</td>"
        "</tr>"
        for key, value in sorted((payload.get("issue_type_counts") or {}).items())
    )
    target_count_rows = "".join(
        "<tr>"
        f"<td>{_html_escape(key)}</td>"
        f"<td>{_html_escape(value)}</td>"
        "</tr>"
        for key, value in sorted((payload.get("target_type_counts") or {}).items())
    )
    cards = []
    for item in items:
        context_rows = "".join(
            "<tr>"
            f"<td>{_html_escape(key)}</td>"
            f"<td>{_html_escape(value)}</td>"
            "</tr>"
            for key, value in item.get("context_pairs") or []
        )
        cards.append(
            f"""
      <article class="review-card" data-sequence="{_html_escape(item.get('sequence'))}" data-issue-type="{_html_escape(item.get('issue_type'))}">
        <div class="card-head">
          <div>
            <span class="seq">#{_html_escape(item.get('sequence'))}</span>
            <h2>{_html_escape(item.get('issue_type'))}</h2>
            <p class="muted">{_html_escape(item.get('target_type'))}:{_html_escape(item.get('target_id'))} · gate {_html_escape(item.get('review_gate_status'))}</p>
          </div>
          <strong>{_html_escape(item.get('priority_score'))}</strong>
        </div>
        <p>{_html_escape(item.get('priority_reason'))}</p>
        <p class="excerpt">{_html_escape(item.get('source_context_excerpt'))}</p>
        <details>
          <summary>Evidence</summary>
          <p>{_html_escape(item.get('issue_detail'))}</p>
          <p class="muted">{_html_escape(item.get('suggested_action'))}</p>
          <table><tbody>{context_rows or '<tr><td colspan="2">No compact context.</td></tr>'}</tbody></table>
          <p class="hash">snapshot {_html_escape(item.get('target_snapshot_hash'))}</p>
        </details>
        <div class="decision-row">
          <label>Decision
            <select data-field="decision">
              <option value="">pending</option>
              <option value="approve">approve</option>
              <option value="reject">reject</option>
              <option value="defer">defer</option>
            </select>
          </label>
          <label>Reviewer <input data-field="reviewer_id" placeholder="reviewer id"></label>
          <label>Rationale <input data-field="rationale" placeholder="required before any later import"></label>
        </div>
      </article>
            """
        )
    cards_html = "\n".join(cards) or '<div class="empty">No review seedpack items.</div>'
    allowed = ", ".join(str(value) for value in payload.get("allowed_decisions") or [])
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ontology Review Board</title>
  <style>
    :root {{ --ink:#172033; --muted:#667085; --line:#d9e0ea; --bg:#f6f8fb; --panel:#fff; --ok:#067647; --warn:#b45309; --bad:#b42318; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Segoe UI, Arial, "Malgun Gothic", sans-serif; color:var(--ink); background:var(--bg); }}
    main {{ max-width:1280px; margin:0 auto; padding:22px; }}
    h1 {{ margin:0 0 6px; font-size:26px; }}
    h2 {{ margin:0; font-size:17px; }}
    .muted {{ color:var(--muted); }}
    .topbar {{ position:sticky; top:0; z-index:5; background:rgba(246,248,251,.97); border-bottom:1px solid var(--line); padding:12px 0; backdrop-filter:blur(6px); }}
    .summary {{ display:grid; grid-template-columns:repeat(6, minmax(0,1fr)); gap:10px; margin:14px 0; }}
    .metric {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }}
    .metric span {{ display:block; color:var(--muted); font-size:12px; }}
    .metric strong {{ display:block; margin-top:4px; font-size:22px; }}
    .guardrail {{ border-left:4px solid var(--warn); background:#fff8eb; padding:10px 12px; margin:12px 0; }}
    .toolbar {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:10px; }}
    .toolbar input, .toolbar select, .decision-row input, .decision-row select {{ border:1px solid var(--line); border-radius:6px; padding:8px; background:#fff; }}
    button {{ border:1px solid #b8c2cf; background:#fff; border-radius:6px; padding:8px 10px; cursor:pointer; }}
    .grid {{ display:grid; grid-template-columns:260px minmax(0,1fr); gap:16px; align-items:start; }}
    aside {{ position:sticky; top:116px; }}
    table {{ width:100%; border-collapse:collapse; background:var(--panel); table-layout:fixed; }}
    th, td {{ border:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; overflow-wrap:anywhere; font-size:13px; }}
    th {{ background:#eef2f5; }}
    .review-card {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; margin-bottom:12px; }}
    .card-head {{ display:flex; justify-content:space-between; gap:14px; align-items:flex-start; }}
    .seq {{ display:inline-block; color:#1d4ed8; font-weight:700; margin-bottom:4px; }}
    .excerpt {{ background:#f8fafc; border:1px solid #e5e7eb; border-radius:6px; padding:10px; }}
    .decision-row {{ display:grid; grid-template-columns:160px 180px minmax(220px,1fr); gap:8px; margin-top:12px; }}
    .decision-row label {{ display:flex; flex-direction:column; gap:4px; color:var(--muted); font-size:12px; }}
    .hash {{ font-family:Consolas, monospace; font-size:12px; color:var(--muted); }}
    .empty {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; }}
    @media (max-width:860px) {{ main {{ padding:12px; }} .summary, .grid, .decision-row {{ grid-template-columns:1fr; }} aside {{ position:static; }} }}
  </style>
</head>
<body>
<main>
  <section class="topbar">
    <p class="muted">{_html_escape(source_path.name)}</p>
    <h1>Ontology Review Board</h1>
    <div class="toolbar">
      <input id="search" placeholder="filter text">
      <select id="issueFilter"><option value="">all issue types</option></select>
      <button id="exportJson">Export decisions JSONL</button>
      <span class="muted" id="visibleCount"></span>
    </div>
  </section>
  <section class="summary">
    <div class="metric"><span>items</span><strong>{_html_escape(payload.get('item_count'))}</strong></div>
    <div class="metric"><span>db writes</span><strong>{_html_escape(payload.get('db_writes'))}</strong></div>
    <div class="metric"><span>status update</span><strong>{_html_escape(payload.get('status_update_allowed'))}</strong></div>
    <div class="metric"><span>approval claim</span><strong>{_html_escape(payload.get('approval_claim'))}</strong></div>
    <div class="metric"><span>allowed decisions</span><strong>{_html_escape(allowed)}</strong></div>
    <div class="metric"><span>seedpack</span><strong>{_html_escape((payload.get('batch') or {}).get('seedpack_id'))}</strong></div>
  </section>
  <div class="guardrail">Read-only review workspace. Decisions exported here are reviewer notes only; no raw KSA, concept, link, or review status is changed by this page.</div>
  <section class="grid">
    <aside>
      <h2>Issue Types</h2>
      <table><tbody>{issue_count_rows or '<tr><td colspan="2">No issue counts.</td></tr>'}</tbody></table>
      <h2 style="margin-top:16px">Targets</h2>
      <table><tbody>{target_count_rows or '<tr><td colspan="2">No target counts.</td></tr>'}</tbody></table>
    </aside>
    <section id="cards">{cards_html}</section>
  </section>
</main>
<script>
const cards = Array.from(document.querySelectorAll('.review-card'));
const search = document.getElementById('search');
const issueFilter = document.getElementById('issueFilter');
const visibleCount = document.getElementById('visibleCount');
const issueTypes = Array.from(new Set(cards.map(card => card.dataset.issueType).filter(Boolean))).sort();
for (const issue of issueTypes) {{
  const option = document.createElement('option');
  option.value = issue;
  option.textContent = issue;
  issueFilter.appendChild(option);
}}
function applyFilter() {{
  const text = search.value.trim().toLowerCase();
  const issue = issueFilter.value;
  let visible = 0;
  for (const card of cards) {{
    const matchesText = !text || card.textContent.toLowerCase().includes(text);
    const matchesIssue = !issue || card.dataset.issueType === issue;
    const show = matchesText && matchesIssue;
    card.style.display = show ? '' : 'none';
    if (show) visible += 1;
  }}
  visibleCount.textContent = `${{visible}} visible`;
}}
function cardDecision(card) {{
  const get = name => {{
    const el = card.querySelector(`[data-field="${{name}}"]`);
    return el ? el.value.trim() : '';
  }};
  return {{
    sequence: Number(card.dataset.sequence || 0),
    issue_type: card.dataset.issueType || '',
    decision: get('decision'),
    reviewer_id: get('reviewer_id'),
    rationale: get('rationale')
  }};
}}
document.getElementById('exportJson').addEventListener('click', () => {{
  const rows = cards.map(cardDecision).filter(row => row.decision || row.reviewer_id || row.rationale);
  const blob = new Blob(rows.map(row => JSON.stringify(row)).join('\\n') ? [rows.map(row => JSON.stringify(row)).join('\\n') + '\\n'] : [''], {{ type:'application/jsonl;charset=utf-8' }});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'ontology_review_decisions.jsonl';
  a.click();
  URL.revokeObjectURL(url);
}});
search.addEventListener('input', applyFilter);
issueFilter.addEventListener('change', applyFilter);
applyFilter();
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "NcsDashboard/0.3"

    def json_response(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def html_response(self) -> None:
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def aihr_demo_response(self) -> None:
        demo_path = resolve_aihr_demo_html_path()
        if demo_path is None:
            self.json_response(
                {
                    "error": "aihr_demo_not_found",
                    "expected_glob": _public_aihr_path_text(
                        ROOT / "reports" / AIHR_DEMO_HTML_GLOB
                    ),
                    "hint": "Run scripts\\ncs_harness.py run-aihr-plan-demo --out-dir reports --base-name aihr_plan_demo_<date> first.",
                },
                status=404,
            )
            return
        body = demo_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def aihr_live_response(self) -> None:
        body = render_aihr_live_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def aihr_training_system_builder_response(self) -> None:
        body = render_aihr_training_system_builder_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_aihr_json_artifact(self, path: Path, error_code: str) -> dict | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.json_response(
                {
                    "error": error_code,
                    "path": _public_aihr_path_text(path),
                    "detail": str(exc),
                },
                status=400,
            )
            return None
        except OSError as exc:
            self.json_response(
                {
                    "error": error_code,
                    "path": _public_aihr_path_text(path),
                    "detail": str(exc),
                },
                status=400,
            )
            return None
        if not isinstance(payload, dict):
            self.json_response(
                {
                    "error": error_code,
                    "path": _public_aihr_path_text(path),
                    "detail": f"expected JSON object, got {type(payload).__name__}",
                },
                status=400,
            )
            return None
        return payload

    def aihr_readiness_response(self) -> None:
        readiness_path = resolve_aihr_readiness_json_path()
        if readiness_path is None:
            self.json_response(
                {
                    "error": "aihr_readiness_not_found",
                    "expected_glob": _public_aihr_path_text(
                        ROOT / "reports" / AIHR_READINESS_JSON_GLOB
                    ),
                    "hint": "Run scripts\\release_readiness_report.py with --demo-json and --demo-html inputs first.",
                },
                status=404,
            )
            return
        payload = self.read_aihr_json_artifact(readiness_path, "aihr_readiness_invalid")
        if payload is None:
            return
        triage_path = resolve_aihr_review_triage_json_path()
        triage_payload = None
        if triage_path is not None:
            try:
                triage_payload = json.loads(triage_path.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                triage_payload = None
        body = render_aihr_readiness_html(
            payload,
            readiness_path,
            triage_payload=triage_payload,
            triage_path=triage_path,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def aihr_readiness_json_response(self) -> None:
        readiness_path = resolve_aihr_readiness_json_path()
        if readiness_path is None:
            self.json_response(
                {
                    "error": "aihr_readiness_not_found",
                    "expected_glob": _public_aihr_path_text(
                        ROOT / "reports" / AIHR_READINESS_JSON_GLOB
                    ),
                    "hint": "Run scripts\\release_readiness_report.py with --demo-json and --demo-html inputs first.",
                },
                status=404,
            )
            return
        payload = self.read_aihr_json_artifact(readiness_path, "aihr_readiness_invalid")
        if payload is None:
            return
        self.json_response(public_aihr_dashboard_payload(payload))

    def aihr_review_board_response(self) -> None:
        triage_path = resolve_aihr_review_triage_json_path()
        if triage_path is None:
            self.json_response(
                {
                    "error": "aihr_review_triage_not_found",
                    "expected_glob": _public_aihr_path_text(
                        ROOT / "reports" / AIHR_REVIEW_TRIAGE_JSON_GLOB
                    ),
                    "hint": "Run scripts\\ncs_harness.py review-triage with AI-HR quality/review/transition artifacts first.",
                },
                status=404,
            )
            return
        payload = self.read_aihr_json_artifact(triage_path, "aihr_review_triage_invalid")
        if payload is None:
            return
        body = render_aihr_review_board_html(payload, triage_path).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def aihr_review_board_json_response(self) -> None:
        triage_path = resolve_aihr_review_triage_json_path()
        if triage_path is None:
            self.json_response(
                {
                    "error": "aihr_review_triage_not_found",
                    "expected_glob": _public_aihr_path_text(
                        ROOT / "reports" / AIHR_REVIEW_TRIAGE_JSON_GLOB
                    ),
                    "hint": "Run scripts\\ncs_harness.py review-triage with AI-HR quality/review/transition artifacts first.",
                },
                status=404,
            )
            return
        payload = self.read_aihr_json_artifact(triage_path, "aihr_review_triage_invalid")
        if payload is None:
            return
        self.json_response(public_aihr_dashboard_payload(payload))

    def aihr_provenance_reconfirmation_response(self) -> None:
        packet_path = resolve_aihr_provenance_reconfirmation_json_path()
        if packet_path is None:
            self.json_response(
                {
                    "error": "aihr_provenance_reconfirmation_not_found",
                    "expected_globs": [
                        _public_aihr_path_text(ROOT / "reports" / glob)
                        for glob in AIHR_PROVENANCE_RECONFIRMATION_JSON_GLOBS
                    ],
                    "hint": (
                        "Run scripts\\ncs_harness.py "
                        "export-human-review-provenance-reconfirmation-proofset first."
                    ),
                },
                status=404,
            )
            return
        payload = self.read_aihr_json_artifact(
            packet_path,
            "aihr_provenance_reconfirmation_invalid",
        )
        if payload is None:
            return
        body = render_aihr_provenance_reconfirmation_html(payload, packet_path).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def aihr_provenance_reconfirmation_json_response(self) -> None:
        packet_path = resolve_aihr_provenance_reconfirmation_json_path()
        if packet_path is None:
            self.json_response(
                {
                    "error": "aihr_provenance_reconfirmation_not_found",
                    "expected_globs": [
                        _public_aihr_path_text(ROOT / "reports" / glob)
                        for glob in AIHR_PROVENANCE_RECONFIRMATION_JSON_GLOBS
                    ],
                    "hint": (
                        "Run scripts\\ncs_harness.py "
                        "export-human-review-provenance-reconfirmation-proofset first."
                    ),
                },
                status=404,
            )
            return
        payload = self.read_aihr_json_artifact(
            packet_path,
            "aihr_provenance_reconfirmation_invalid",
        )
        if payload is None:
            return
        self.json_response(public_aihr_provenance_reconfirmation_payload(payload))

    def ontology_review_board_response(self) -> None:
        seedpack_path = resolve_review_seedpack_jsonl_path()
        if seedpack_path is None:
            self.json_response(
                {
                    "error": "review_seedpack_not_found",
                    "expected_globs": [
                        _public_aihr_path_text(ROOT / "reports" / glob)
                        for glob in REVIEW_SEEDPACK_JSONL_GLOBS
                    ],
                    "hint": "Run scripts\\ncs_harness.py export-review-seedpack first.",
                },
                status=404,
            )
            return
        try:
            payload = load_review_seedpack_payload(seedpack_path)
        except OSError as exc:
            self.json_response(
                {
                    "error": "review_seedpack_unreadable",
                    "path": _public_aihr_path_text(seedpack_path),
                    "detail": type(exc).__name__,
                },
                status=400,
            )
            return
        body = render_ontology_review_board_html(payload, seedpack_path).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def ontology_review_board_json_response(self) -> None:
        seedpack_path = resolve_review_seedpack_jsonl_path()
        if seedpack_path is None:
            self.json_response(
                {
                    "error": "review_seedpack_not_found",
                    "expected_globs": [
                        _public_aihr_path_text(ROOT / "reports" / glob)
                        for glob in REVIEW_SEEDPACK_JSONL_GLOBS
                    ],
                    "hint": "Run scripts\\ncs_harness.py export-review-seedpack first.",
                },
                status=404,
            )
            return
        try:
            payload = load_review_seedpack_payload(seedpack_path)
        except OSError as exc:
            self.json_response(
                {
                    "error": "review_seedpack_unreadable",
                    "path": _public_aihr_path_text(seedpack_path),
                    "detail": type(exc).__name__,
                },
                status=400,
            )
            return
        self.json_response(public_aihr_dashboard_payload(payload))

    def ksa_review_html_artifact_response(self, glob: str, artifact_label: str) -> None:
        artifact_path = resolve_ksa_review_html_path(glob)
        if artifact_path is None:
            self.json_response(
                {
                    "error": "ksa_review_artifact_not_found",
                    "artifact": artifact_label,
                    "expected_glob": _public_aihr_path_text(ROOT / "reports" / glob),
                    "hint": "Run the KSA LLM review triage and seedpack generation first.",
                },
                status=404,
            )
            return
        body = artifact_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def aihr_agent_queue_response(self) -> None:
        queue_path = resolve_aihr_agent_queue_json_path()
        if queue_path is None:
            self.json_response(
                {
                    "error": "aihr_agent_queue_not_found",
                    "expected_glob": aihr_agent_queue_expected_globs(),
                    "hint": "Run scripts\\release_readiness_report.py with --agent-queue-out first.",
                },
                status=404,
            )
            return
        payload = self.read_aihr_json_artifact(queue_path, "aihr_agent_queue_invalid")
        if payload is None:
            return
        body = render_aihr_agent_queue_html(payload, queue_path).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def aihr_agent_queue_status_response(self) -> None:
        status_path = resolve_aihr_agent_queue_status_json_path()
        if status_path is None:
            self.json_response(
                {
                    "error": "aihr_agent_queue_status_not_found",
                    "expected_glob": _public_aihr_path_text(
                        ROOT / "reports" / AIHR_AGENT_QUEUE_STATUS_JSON_GLOB
                    ),
                    "hint": "Run scripts\\ncs_harness.py agent-queue-status with --queue and --out first.",
                },
                status=404,
            )
            return
        payload = self.read_aihr_json_artifact(status_path, "aihr_agent_queue_status_invalid")
        if payload is None:
            return
        payload = sanitize_aihr_agent_queue_public_paths(copy.deepcopy(payload))
        body = render_aihr_agent_queue_status_html(payload, status_path).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def aihr_agent_queue_status_json_response(self) -> None:
        status_path = resolve_aihr_agent_queue_status_json_path()
        if status_path is None:
            self.json_response(
                {
                    "error": "aihr_agent_queue_status_not_found",
                    "expected_glob": _public_aihr_path_text(
                        ROOT / "reports" / AIHR_AGENT_QUEUE_STATUS_JSON_GLOB
                    ),
                    "hint": "Run scripts\\ncs_harness.py agent-queue-status with --queue and --out first.",
                },
                status=404,
            )
            return
        payload = self.read_aihr_json_artifact(status_path, "aihr_agent_queue_status_invalid")
        if payload is None:
            return
        self.json_response(sanitize_aihr_agent_queue_public_paths(copy.deepcopy(payload)))

    def aihr_agent_queue_run_response(self) -> None:
        run_path = resolve_aihr_agent_queue_run_json_path()
        if run_path is None:
            self.json_response(
                {
                    "error": "aihr_agent_queue_run_not_found",
                    "expected_glob": _public_aihr_path_text(
                        ROOT / "reports" / AIHR_AGENT_QUEUE_RUN_JSON_GLOB
                    ),
                    "hint": "Run scripts\\ncs_harness.py agent-queue-run-ready with --out first.",
                },
                status=404,
            )
            return
        payload = self.read_aihr_json_artifact(run_path, "aihr_agent_queue_run_invalid")
        if payload is None:
            return
        if not is_actual_aihr_agent_queue_run(payload, run_path):
            self.json_response(
                {
                    "error": "aihr_agent_queue_run_not_actual",
                    "path": _public_aihr_path_text(run_path),
                    "summary": payload.get("summary") if isinstance(payload, dict) else {},
                    "hint": "Run scripts\\ncs_harness.py agent-queue-run-ready without --dry-run.",
                },
                status=409,
            )
            return
        artifact_issues = aihr_agent_queue_run_artifact_issues(payload, run_path)
        if artifact_issues:
            self.json_response(
                {
                    "error": "aihr_agent_queue_run_stale_or_unlinked",
                    "path": _public_aihr_path_text(run_path),
                    "issues": artifact_issues,
                    "summary": payload.get("summary") if isinstance(payload, dict) else {},
                    "hint": "Regenerate the queue run artifact from the current agent queue.",
                },
                status=409,
            )
            return
        payload = sanitize_aihr_agent_queue_public_paths(copy.deepcopy(payload))
        body = render_aihr_agent_queue_run_html(payload, run_path).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def aihr_agent_queue_run_json_response(self) -> None:
        run_path = resolve_aihr_agent_queue_run_json_path()
        if run_path is None:
            self.json_response(
                {
                    "error": "aihr_agent_queue_run_not_found",
                    "expected_glob": _public_aihr_path_text(
                        ROOT / "reports" / AIHR_AGENT_QUEUE_RUN_JSON_GLOB
                    ),
                    "hint": "Run scripts\\ncs_harness.py agent-queue-run-ready with --out first.",
                },
                status=404,
            )
            return
        payload = self.read_aihr_json_artifact(run_path, "aihr_agent_queue_run_invalid")
        if payload is None:
            return
        if not is_actual_aihr_agent_queue_run(payload, run_path):
            self.json_response(
                {
                    "error": "aihr_agent_queue_run_not_actual",
                    "path": _public_aihr_path_text(run_path),
                    "summary": payload.get("summary") if isinstance(payload, dict) else {},
                    "hint": "Run scripts\\ncs_harness.py agent-queue-run-ready without --dry-run.",
                },
                status=409,
            )
            return
        artifact_issues = aihr_agent_queue_run_artifact_issues(payload, run_path)
        if artifact_issues:
            self.json_response(
                {
                    "error": "aihr_agent_queue_run_stale_or_unlinked",
                    "path": _public_aihr_path_text(run_path),
                    "issues": artifact_issues,
                    "summary": payload.get("summary") if isinstance(payload, dict) else {},
                    "hint": "Regenerate the queue run artifact from the current agent queue.",
                },
                status=409,
            )
            return
        self.json_response(sanitize_aihr_agent_queue_run_payload(payload))

    def ksa_definitions_response(self) -> None:
        body = render_ksa_definition_dashboard_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def ksa_review_dashboard_response(self) -> None:
        body = render_ksa_review_dashboard_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def ksa_label_patterns_response(self) -> None:
        body = render_ksa_label_patterns_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def ksa_label_auto_triage_response(self) -> None:
        body = render_ksa_label_auto_triage_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def ksa_review_status_json_response(self) -> None:
        self.json_response(get_ksa_preprocessing_review_status())

    def ksa_preprocessing_dashboard_response(self) -> None:
        body = render_ksa_preprocessing_dashboard_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def ncs_knowledge_graph_response(self) -> None:
        if not NCS_KNOWLEDGE_GRAPH_HTML.exists():
            self.json_response(
                {
                    "error": "ncs_knowledge_graph_page_missing",
                    "detail": "The NCS knowledge graph page asset is missing.",
                },
                status=404,
            )
            return
        body = NCS_KNOWLEDGE_GRAPH_HTML.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def ncs_3d_force_graph_asset_response(self) -> None:
        if not NCS_3D_FORCE_GRAPH_JS.exists():
            self.json_response(
                {
                    "error": "ncs_3d_renderer_missing",
                    "detail": "The vendored 3D graph renderer is missing.",
                },
                status=404,
            )
            return
        body = NCS_3D_FORCE_GRAPH_JS.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def ncs_knowledge_graph_json_response(
        self,
        params: dict[str, list[str]],
    ) -> None:
        try:
            payload = build_ncs_knowledge_graph(
                self.server.db_path,
                query=first(params, "q"),
                unit_code=first(params, "unit_code"),
                major_code=first(params, "major_code"),
                classification_id=first(params, "classification_id"),
                max_nodes=first(params, "max_nodes", "72"),
            )
        except KnowledgeGraphDataError as exc:
            if exc.code in {"database_missing", "major_not_found"}:
                status = 404
            elif exc.code == "classification_invalid":
                status = 400
            else:
                status = 503
            self.json_response(exc.to_payload(), status=status)
            return
        status = 200 if payload.get("ok") else 404
        self.json_response(payload, status=status)

    def query_router_response(self) -> None:
        body = render_query_router_samples_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                self.html_response()
            elif parsed.path == "/ncs-knowledge-graph":
                self.ncs_knowledge_graph_response()
            elif parsed.path == "/assets/3d-force-graph-1.80.0.min.js":
                self.ncs_3d_force_graph_asset_response()
            elif parsed.path == "/api/ncs-knowledge-graph":
                self.ncs_knowledge_graph_json_response(params)
            elif parsed.path == "/aihr-live":
                self.aihr_live_response()
            elif parsed.path == "/aihr-training-system-builder":
                self.aihr_training_system_builder_response()
            elif parsed.path == "/aihr-plan-demo":
                self.aihr_demo_response()
            elif parsed.path == "/aihr-readiness":
                self.aihr_readiness_response()
            elif parsed.path == "/api/aihr-readiness":
                self.aihr_readiness_json_response()
            elif parsed.path == "/aihr-review-board":
                self.aihr_review_board_response()
            elif parsed.path == "/api/aihr-review-board":
                self.aihr_review_board_json_response()
            elif parsed.path == "/aihr-provenance-reconfirmation":
                self.aihr_provenance_reconfirmation_response()
            elif parsed.path == "/api/aihr-provenance-reconfirmation":
                self.aihr_provenance_reconfirmation_json_response()
            elif parsed.path == "/ontology-review-board":
                self.ontology_review_board_response()
            elif parsed.path == "/api/ontology-review-board":
                self.ontology_review_board_json_response()
            elif parsed.path in {"/ksa-definitions", "/ksa-definition-dashboard"}:
                self.ksa_definitions_response()
            elif parsed.path == "/ksa-review-dashboard":
                self.ksa_review_dashboard_response()
            elif parsed.path == "/ksa-label-patterns":
                self.ksa_label_patterns_response()
            elif parsed.path == "/ksa-label-auto-triage":
                self.ksa_label_auto_triage_response()
            elif parsed.path == "/api/ksa-review-status":
                self.ksa_review_status_json_response()
            elif parsed.path == "/ksa-preprocessing-dashboard":
                self.ksa_preprocessing_dashboard_response()
            elif parsed.path == "/ksa-label-needs-review-seedpack":
                self.ksa_review_html_artifact_response(
                    KSA_LABEL_NEEDS_REVIEW_HTML_GLOB,
                    "ksa_label_needs_review_seedpack",
                )
            elif parsed.path == "/ksa-meaning-needs-review-seedpack":
                self.ksa_review_html_artifact_response(
                    KSA_MEANING_NEEDS_REVIEW_HTML_GLOB,
                    "ksa_meaning_needs_review_seedpack",
                )
            elif parsed.path == "/ksa-meaning-missing-scoped-seedpack":
                self.ksa_review_html_artifact_response(
                    KSA_MEANING_MISSING_SCOPED_HTML_GLOB,
                    "ksa_meaning_missing_scoped_seedpack",
                )
            elif parsed.path == "/ksa-preprocessing-pipeline-status":
                self.ksa_review_html_artifact_response(
                    KSA_PREPROCESSING_PIPELINE_HTML_GLOB,
                    "ksa_preprocessing_pipeline_status",
                )
            elif parsed.path == "/aihr-query-router":
                self.query_router_response()
            elif parsed.path == "/aihr-agent-queue":
                self.aihr_agent_queue_response()
            elif parsed.path == "/aihr-agent-queue-status":
                self.aihr_agent_queue_status_response()
            elif parsed.path == "/api/aihr-agent-queue-status":
                self.aihr_agent_queue_status_json_response()
            elif parsed.path == "/aihr-agent-queue-run":
                self.aihr_agent_queue_run_response()
            elif parsed.path == "/api/aihr-agent-queue-run":
                self.aihr_agent_queue_run_json_response()
            elif parsed.path == "/api/status":
                self.json_response(get_status(self.server.db_path))
            elif parsed.path == "/api/progress":
                self.json_response(get_progress(self.server.db_path, params))
            elif parsed.path == "/api/workbench":
                self.json_response(get_workbench(self.server.db_path, params))
            elif parsed.path == "/api/items":
                self.json_response(get_items(self.server.db_path, params))
            elif parsed.path == "/api/item-detail":
                self.json_response(get_item_detail(self.server.db_path, params))
            elif parsed.path == "/api/taxonomy":
                self.json_response(get_taxonomy(self.server.db_path, params))
            elif parsed.path == "/api/ontology":
                self.json_response(get_ontology(self.server.db_path, params))
            elif parsed.path == "/api/ontology-status":
                self.json_response(get_ontology_status(self.server.db_path, params))
            elif parsed.path == "/api/ksa-definitions":
                self.json_response(get_ksa_definitions(self.server.db_path, params))
            elif parsed.path == "/api/ksa-label-patterns":
                self.json_response(get_ksa_label_patterns(self.server.db_path, params))
            elif parsed.path == "/api/ksa-label-auto-triage":
                self.json_response(get_ksa_label_auto_triage(self.server.db_path, params))
            elif parsed.path == "/api/concepts":
                self.json_response(get_concepts(self.server.db_path, params))
            elif parsed.path == "/api/recommendation-runs":
                self.json_response(get_recommendation_runs(self.server.db_path, params))
            elif parsed.path == "/api/recommendation-detail":
                self.json_response(get_recommendation_detail(self.server.db_path, params))
            elif parsed.path == "/api/classifications":
                self.json_response(get_classifications(self.server.db_path, params))
            elif parsed.path == "/api/units":
                self.json_response(get_units(self.server.db_path, params))
            elif parsed.path == "/api/unit":
                self.json_response(get_unit_detail(self.server.db_path, params))
            elif parsed.path == "/api/api-orphans":
                self.json_response(get_api_orphans(self.server.db_path, params))
            elif parsed.path == "/api/issues":
                self.json_response(get_issues(self.server.db_path, params))
            else:
                self.json_response({"error": "not_found"}, status=404)
        except Exception as exc:
            self.json_response({"error": type(exc).__name__, "detail": str(exc)}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = self.rfile.read(length).decode("utf-8") if length else "{}"
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.json_response(
                    {
                        "error": "invalid_json_body",
                        "detail": str(exc),
                    },
                    status=400,
                )
                return
            if not isinstance(payload, dict):
                self.json_response(
                    {
                        "error": "invalid_json_body",
                        "detail": "Request body must be a JSON object.",
                    },
                    status=400,
                )
                return
            if parsed.path == "/api/preprocess":
                self.json_response(save_manual_preprocess(self.server.db_path, payload))
            elif parsed.path == "/api/refine":
                self.json_response(save_refined(self.server.db_path, payload))
            elif parsed.path == "/api/resolve":
                self.json_response(resolve_issue(self.server.db_path, payload))
            elif parsed.path == "/api/review-mapping":
                result = review_mapping_candidate(self.server.db_path, payload)
                self.json_response(result, status=200 if result.get("ok") else 400)
            elif parsed.path == "/api/review-refinement":
                self.json_response(review_refinement_job(self.server.db_path, payload))
            elif parsed.path == "/api/ksa-label-review":
                result = review_ksa_label_candidate(self.server.db_path, payload)
                self.json_response(result, status=200 if result.get("ok") else 400)
            elif parsed.path == "/api/ksa-label-edit":
                result = edit_ksa_label_candidate(self.server.db_path, payload)
                self.json_response(result, status=200 if result.get("ok") else 400)
            elif parsed.path == "/api/ksa-meaning-review":
                result = review_ksa_meaning_candidate(self.server.db_path, payload)
                self.json_response(result, status=200 if result.get("ok") else 400)
            elif parsed.path == "/api/aihr-plan":
                result = build_aihr_live_plan(self.server.db_path, payload)
                self.json_response(result, status=200 if result.get("ok") else 400)
            else:
                self.json_response({"error": "not_found"}, status=404)
        except Exception as exc:
            self.json_response({"error": type(exc).__name__, "detail": str(exc)}, status=500)

    def log_message(self, format: str, *args) -> None:
        return


def _readable_schema_has_tables(path: Path, tables: tuple[str, ...]) -> bool:
    if not path.exists():
        return False
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30)
    except sqlite3.Error:
        return False
    try:
        for table in tables:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if row is None:
                return False
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return True


def prepare_dashboard_db(db_path: Path, *, prepare_ontology: bool = False) -> None:
    path = Path(db_path).resolve()
    if path in _DB_SCHEMA_PREPARED_PATHS and (
        not prepare_ontology or path in _DB_ONTOLOGY_PREPARED_PATHS
    ):
        return
    with _DB_PREPARE_LOCK:
        schema_ready = path in _DB_SCHEMA_PREPARED_PATHS
        ontology_ready = path in _DB_ONTOLOGY_PREPARED_PATHS
        if not schema_ready and _readable_schema_has_tables(path, _DASHBOARD_SCHEMA_READY_TABLES):
            _DB_SCHEMA_PREPARED_PATHS.add(path)
            schema_ready = True
        if (
            prepare_ontology
            and not ontology_ready
            and _readable_schema_has_tables(path, _DASHBOARD_ONTOLOGY_READY_TABLES)
        ):
            _DB_ONTOLOGY_PREPARED_PATHS.add(path)
            ontology_ready = True
        if schema_ready and (not prepare_ontology or ontology_ready):
            return
        conn = connect(path)
        try:
            if not schema_ready:
                initialize_database(conn)
                _DB_SCHEMA_PREPARED_PATHS.add(path)
            if prepare_ontology and not ontology_ready:
                ensure_ontology_seeded(conn)
                _DB_ONTOLOGY_PREPARED_PATHS.add(path)
        finally:
            conn.close()


def connect_db(db_path: Path, *, prepare_ontology: bool = False):
    prepare_dashboard_db(db_path, prepare_ontology=prepare_ontology)
    return connect(db_path)


def connect_db_readonly(db_path: Path):
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def scalar(conn, sql: str, params: list | tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def connect_db_for_read(
    db_path: Path,
    *,
    required_tables: tuple[str, ...] = (),
):
    path = Path(db_path)
    if not path.exists():
        raise DashboardReadOnlyError(
            "database_missing",
            f"Database does not exist: {path}",
        )
    try:
        conn = connect_db_readonly(path)
    except sqlite3.Error as exc:
        raise DashboardReadOnlyError(
            "database_unreadable",
            f"Database cannot be opened read-only: {type(exc).__name__}: {exc}",
        ) from exc
    try:
        missing_tables = [table for table in required_tables if not table_exists(conn, table)]
    except sqlite3.Error as exc:
        conn.close()
        raise DashboardReadOnlyError(
            "database_unreadable",
            f"Database schema cannot be inspected read-only: {type(exc).__name__}: {exc}",
        ) from exc
    if missing_tables:
        conn.close()
        raise DashboardReadOnlyError(
            "schema_incomplete",
            "Database is missing tables required by this read endpoint.",
            missing_tables=missing_tables,
        )
    return conn


def first(params: dict[str, list[str]], key: str, default: str = "") -> str:
    return params.get(key, [default])[0].strip()


def safe_limit(params: dict[str, list[str]], default: int = 100, maximum: int = 500) -> int:
    try:
        return max(1, min(int(first(params, "limit", str(default))), maximum))
    except ValueError:
        return default


def classification_filters(params: dict[str, list[str]], alias: str = "c") -> tuple[list[str], list[str]]:
    clauses: list[str] = []
    values: list[str] = []
    for field in ["major_code", "middle_code", "small_code", "sub_code"]:
        value = first(params, field)
        if value:
            clauses.append(f"{alias}.{field} = ?")
            values.append(value)
    return clauses, values


def scoped_where(params: dict[str, list[str]], alias: str = "c", extra: list[str] | None = None) -> tuple[str, list[str]]:
    clauses, values = classification_filters(params, alias)
    if extra:
        clauses.extend(extra)
    return (f"WHERE {' AND '.join(clauses)}" if clauses else "", values)


KSA_LABEL_PATTERN_GROUP_META = {
    "already_human_reviewed": {
        "label": "이미 사람확인",
        "decision_hint": "이미 사람 감사 로그가 있는 라벨입니다. 추가 자동 승인은 하지 않습니다.",
        "risk": "done",
    },
    "seed_approved_same_label": {
        "label": "01 승인 라벨과 동일",
        "decision_hint": "01 대분류에서 사람이 확인한 동일 라벨입니다. 묶음 승인 후보로 볼 수 있습니다.",
        "risk": "low",
    },
    "current_needs_review": {
        "label": "현재 수정/거절 대상",
        "decision_hint": "이미 needs_review/rejected 상태입니다. 빠른 일괄 승인에서 제외합니다.",
        "risk": "high",
    },
    "seed_hold_same_label": {
        "label": "01 보류 라벨과 동일",
        "decision_hint": "01 대분류에서 수정/보류된 동일 라벨입니다. 별도 검토가 필요합니다.",
        "risk": "high",
    },
    "generic_or_short": {
        "label": "너무 일반적이거나 짧음",
        "decision_hint": "관리/이해/수행 같은 일반 라벨입니다. 사람확인 전에 맥락 확인이 필요합니다.",
        "risk": "medium",
    },
    "domain_review_first": {
        "label": "전문분야 우선 검토",
        "decision_hint": "건설/기계/화학/전기/안전 등 도메인 정확성 확인이 필요한 범위입니다.",
        "risk": "medium",
    },
    "low_risk_already_short": {
        "label": "원문 자체가 짧은 라벨",
        "decision_hint": "원문 KSA가 이미 짧아 라벨화 위험이 낮습니다. 샘플 검토 후 묶음 후보로 볼 수 있습니다.",
        "risk": "low",
    },
    "unclassified_review_candidate": {
        "label": "미분류 검토 후보",
        "decision_hint": "01 기준과 직접 매칭되지 않은 후보입니다. 추가 샘플 리뷰가 필요합니다.",
        "risk": "medium",
    },
}


def _ksa_label_pattern_cte(
    target_where: str,
    *,
    generic_placeholder_sql: str,
    domain_placeholder_sql: str,
) -> str:
    human_status_sql = ", ".join(f"'{status}'" for status in KSA_LABEL_PATTERN_HUMAN_STATUSES)
    return f"""
    WITH seed_human AS (
      SELECT DISTINCT label.normalized_label_key
      FROM ontology_concept_label_candidates label
      JOIN ksa_items ki ON ki.ksa_id = label.source_ksa_id
      JOIN competency_elements ce ON ce.element_id = ki.element_id
      JOIN competency_units cu ON cu.unit_code = ce.unit_code
      JOIN classifications c ON c.classification_id = cu.classification_id
      WHERE c.major_code = ?
        AND label.review_status IN ({human_status_sql})
        AND EXISTS (
          SELECT 1
          FROM review_audit_log audit
          WHERE audit.entity_type = 'ontology_concept_label_candidate'
            AND audit.entity_id = CAST(label.label_id AS TEXT)
            AND audit.new_status = label.review_status
        )
    ),
    seed_hold AS (
      SELECT DISTINCT label.normalized_label_key
      FROM ontology_concept_label_candidates label
      JOIN ksa_items ki ON ki.ksa_id = label.source_ksa_id
      JOIN competency_elements ce ON ce.element_id = ki.element_id
      JOIN competency_units cu ON cu.unit_code = ce.unit_code
      JOIN classifications c ON c.classification_id = cu.classification_id
      WHERE c.major_code = ?
        AND label.review_status IN ('needs_review', 'rejected')
    ),
    scoped_labels AS (
      SELECT
        label.label_id,
        label.concept_id,
        label.source_ksa_id,
        label.concept_type,
        label.source_text,
        label.label_text,
        label.normalized_label_key,
        label.source_method,
        label.review_status,
        c.major_code,
        c.major_name,
        c.middle_code,
        c.middle_name,
        c.small_code,
        c.small_name,
        c.sub_code,
        c.sub_name,
        cu.unit_code,
        cu.unit_name_raw,
        CASE WHEN seed_human.normalized_label_key IS NOT NULL THEN 1 ELSE 0 END AS seed_human_match,
        CASE WHEN seed_hold.normalized_label_key IS NOT NULL THEN 1 ELSE 0 END AS seed_hold_match
      FROM ontology_concept_label_candidates label
      JOIN ksa_items ki ON ki.ksa_id = label.source_ksa_id
      JOIN competency_elements ce ON ce.element_id = ki.element_id
      JOIN competency_units cu ON cu.unit_code = ce.unit_code
      JOIN classifications c ON c.classification_id = cu.classification_id
      LEFT JOIN seed_human ON seed_human.normalized_label_key = label.normalized_label_key
      LEFT JOIN seed_hold ON seed_hold.normalized_label_key = label.normalized_label_key
      {target_where}
    ),
    classified AS (
      SELECT
        *,
        CASE
          WHEN review_status IN ({human_status_sql}) THEN 'already_human_reviewed'
          WHEN seed_human_match = 1 THEN 'seed_approved_same_label'
          WHEN review_status IN ('needs_review', 'rejected') THEN 'current_needs_review'
          WHEN seed_hold_match = 1 THEN 'seed_hold_same_label'
          WHEN TRIM(label_text) IN ({generic_placeholder_sql}) OR LENGTH(TRIM(label_text)) <= 2 THEN 'generic_or_short'
          WHEN major_code IN ({domain_placeholder_sql}) THEN 'domain_review_first'
          WHEN source_method = 'already_short_label' THEN 'low_risk_already_short'
          ELSE 'unclassified_review_candidate'
        END AS pattern_group
      FROM scoped_labels
    )
    """


def _ksa_label_pattern_base_values(
    seed_major_code: str,
    target_values: list[str],
) -> list[object]:
    return [
        seed_major_code,
        seed_major_code,
        *target_values,
        *KSA_LABEL_PATTERN_GENERIC_LABELS,
        *KSA_LABEL_PATTERN_DOMAIN_MAJOR_CODES,
    ]


def get_ksa_label_patterns(db_path: Path, params: dict[str, list[str]]) -> dict:
    try:
        conn = connect_db_for_read(
            db_path,
            required_tables=KSA_LABEL_PATTERN_TABLES,
        )
    except DashboardReadOnlyError as exc:
        return {
            "ok": False,
            "error": exc.to_payload(),
            "generated_at": now_utc(),
            "schema": "ncs_ksa_label_pattern_groups_v1",
        }

    seed_major_code = first(params, "seed_major_code", "01") or "01"
    sample_limit = safe_limit(params, default=5, maximum=20)
    target_where, target_values = scoped_where(params, alias="c")
    generic_placeholder_sql = ", ".join("?" for _ in KSA_LABEL_PATTERN_GENERIC_LABELS)
    domain_placeholder_sql = ", ".join("?" for _ in KSA_LABEL_PATTERN_DOMAIN_MAJOR_CODES)
    cte = _ksa_label_pattern_cte(
        target_where,
        generic_placeholder_sql=generic_placeholder_sql,
        domain_placeholder_sql=domain_placeholder_sql,
    )
    base_values = _ksa_label_pattern_base_values(seed_major_code, target_values)
    try:
        seed_rows = conn.execute(
            """
            SELECT
              label.review_status,
              COUNT(*) AS label_count,
              COUNT(DISTINCT label.normalized_label_key) AS distinct_label_count,
              SUM(CASE WHEN EXISTS (
                SELECT 1
                FROM review_audit_log audit
                WHERE audit.entity_type = 'ontology_concept_label_candidate'
                  AND audit.entity_id = CAST(label.label_id AS TEXT)
                  AND audit.new_status = label.review_status
              ) THEN 1 ELSE 0 END) AS audited_label_count
            FROM ontology_concept_label_candidates label
            JOIN ksa_items ki ON ki.ksa_id = label.source_ksa_id
            JOIN competency_elements ce ON ce.element_id = ki.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE c.major_code = ?
            GROUP BY label.review_status
            ORDER BY label.review_status
            """,
            (seed_major_code,),
        ).fetchall()
        group_rows = conn.execute(
            f"""
            {cte}
            SELECT
              pattern_group,
              COUNT(*) AS label_count,
              COUNT(DISTINCT concept_id) AS concept_count,
              COUNT(DISTINCT normalized_label_key) AS distinct_label_count,
              SUM(CASE WHEN review_status IN ('human_reviewed', 'accepted', 'reviewed') THEN 1 ELSE 0 END) AS human_reviewed_count,
              SUM(CASE WHEN review_status = 'llm_reviewed' THEN 1 ELSE 0 END) AS llm_reviewed_count,
              SUM(CASE WHEN review_status IN ('needs_review', 'rejected') THEN 1 ELSE 0 END) AS needs_review_count
            FROM classified
            GROUP BY pattern_group
            ORDER BY label_count DESC, pattern_group
            """,
            base_values,
        ).fetchall()
        total_label_count = sum(int(row["label_count"] or 0) for row in group_rows)
        groups: list[dict] = []
        for row in group_rows:
            group_id = str(row["pattern_group"])
            meta = KSA_LABEL_PATTERN_GROUP_META.get(group_id, {})
            sample_rows = conn.execute(
                f"""
                {cte}
                SELECT
                  label_id,
                  concept_id,
                  source_ksa_id,
                  concept_type,
                  source_text,
                  label_text,
                  normalized_label_key,
                  source_method,
                  review_status,
                  major_code,
                  major_name,
                  middle_code,
                  middle_name,
                  small_code,
                  small_name,
                  sub_code,
                  sub_name,
                  unit_code,
                  unit_name_raw
                FROM classified
                WHERE pattern_group = ?
                ORDER BY major_code, middle_code, small_code, sub_code, label_id
                LIMIT ?
                """,
                [*base_values, group_id, sample_limit],
            ).fetchall()
            groups.append(
                {
                    "id": group_id,
                    "label": meta.get("label", group_id),
                    "risk": meta.get("risk", "medium"),
                    "decision_hint": meta.get("decision_hint", ""),
                    "label_count": int(row["label_count"] or 0),
                    "concept_count": int(row["concept_count"] or 0),
                    "distinct_label_count": int(row["distinct_label_count"] or 0),
                    "human_reviewed_count": int(row["human_reviewed_count"] or 0),
                    "existing_trusted_status_count": int(row["human_reviewed_count"] or 0),
                    "existing_trusted_status_note": (
                        "Existing trusted review_status values only; not an approval signal."
                    ),
                    "llm_reviewed_count": int(row["llm_reviewed_count"] or 0),
                    "needs_review_count": int(row["needs_review_count"] or 0),
                    "share_percent": round(
                        (int(row["label_count"] or 0) / total_label_count * 100)
                        if total_label_count
                        else 0,
                        1,
                    ),
                    "samples": [dict(sample) for sample in sample_rows],
                }
            )
        seed_status_counts = {
            str(row["review_status"]): {
                "label_count": int(row["label_count"] or 0),
                "distinct_label_count": int(row["distinct_label_count"] or 0),
                "audited_label_count": int(row["audited_label_count"] or 0),
            }
            for row in seed_rows
        }
        seed_human_distinct = sum(
            values["distinct_label_count"]
            for status, values in seed_status_counts.items()
            if status in KSA_LABEL_PATTERN_HUMAN_STATUSES
        )
        seed_audited_human = sum(
            values["audited_label_count"]
            for status, values in seed_status_counts.items()
            if status in KSA_LABEL_PATTERN_HUMAN_STATUSES
        )
        return {
            "ok": True,
            "schema": "ncs_ksa_label_pattern_groups_v1",
            "generated_at": now_utc(),
            "filters": {
                "seed_major_code": seed_major_code,
                "major_code": first(params, "major_code"),
                "middle_code": first(params, "middle_code"),
                "small_code": first(params, "small_code"),
                "sub_code": first(params, "sub_code"),
                "sample_limit": sample_limit,
            },
            "policy": {
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "seed_human_requires_audit_log": True,
                "do_not_auto_set_human_reviewed": True,
                "recommended_use": (
                    "Use the seed major review to classify label candidates into review "
                    "batches. A batch still needs an explicit human decision before any "
                    "trusted status update."
                ),
            },
            "seed_summary": {
                "major_code": seed_major_code,
                "status_counts": seed_status_counts,
                "human_distinct_label_count": seed_human_distinct,
                "audited_human_label_count": seed_audited_human,
                "audited_human_label_note": (
                    "Existing trusted review_status values with audit-log evidence; "
                    "not an approval signal."
                ),
            },
            "summary": {
                "target_label_count": total_label_count,
                "group_count": len(groups),
                "batch_candidate_count": sum(
                    group["label_count"]
                    for group in groups
                    if group["id"] in {"seed_approved_same_label", "low_risk_already_short"}
                ),
                "manual_review_first_count": sum(
                    group["label_count"]
                    for group in groups
                    if group["id"]
                    in {
                        "current_needs_review",
                        "seed_hold_same_label",
                        "generic_or_short",
                        "domain_review_first",
                        "unclassified_review_candidate",
                    }
                ),
            },
            "groups": groups,
        }
    finally:
        conn.close()


def get_ksa_label_auto_triage(db_path: Path, params: dict[str, list[str]]) -> dict:
    safety = {
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_reviewed_written_by_report": False,
        "accepted_written_by_report": False,
        "reviewed_written_by_report": False,
        "llm_reviewed_is_human_approval": False,
        "trusted_sample_scope_is_not_approval": True,
        "trusted_sample_requires_audited_human_review": True,
        "auto_pass_candidate_is_not_human_approval": True,
        "already_trusted_reviewed_bucket_is_not_review_queue": True,
    }

    def sqlite_error_code(exc: sqlite3.Error) -> str:
        message = str(exc).lower()
        if "no such table" in message or "no such column" in message:
            return "schema_incomplete"
        return "read_query_failed"

    def error_payload(code: str, detail: object, **error_fields: object) -> dict:
        error = {"code": code, "detail": detail}
        error.update({key: value for key, value in error_fields.items() if value not in (None, [])})
        return {
            "ok": False,
            "error": error,
            "generated_at": now_utc(),
            "schema": "ksa_label_auto_triage_report_v1",
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "human_reviewed_written_by_report": False,
            "safety": safety,
        }

    try:
        conn = connect_db_for_read(
            db_path,
            required_tables=KSA_LABEL_AUTO_TRIAGE_TABLES,
        )
    except DashboardReadOnlyError as exc:
        return error_payload(
            exc.code or "read_only_error",
            exc.detail,
            missing_tables=exc.missing_tables,
        )
    try:
        return build_ksa_label_auto_triage_report(
            conn,
            major_code=first(params, "major_code") or None,
            middle_code=first(params, "middle_code") or None,
            small_code=first(params, "small_code") or None,
            sub_code=first(params, "sub_code") or None,
            trusted_major_code=first(params, "trusted_major_code", "02") or "02",
            trusted_middle_code=first(params, "trusted_middle_code", "02") or "02",
            trusted_small_code=first(params, "trusted_small_code", "02") or "02",
            limit=safe_limit(params, default=200, maximum=1000),
            sample_limit=safe_limit(params, default=5, maximum=20),
        )
    except sqlite3.Error as exc:
        return error_payload(
            sqlite_error_code(exc),
            f"Auto-triage report cannot be built read-only: {type(exc).__name__}: {exc}",
        )
    finally:
        conn.close()


def ksa_definition_dashboard_error_payload(exc: DashboardReadOnlyError) -> dict:
    return {
        "ok": False,
        "error": exc.to_payload(),
        "generated_at": now_utc(),
        "summary": {
            "matching_ksa": 0,
            "linked_ksa": 0,
            "unlinked_ksa": 0,
            "concepts": 0,
            "concepts_with_definition_text": 0,
            "defined_concepts": 0,
            "missing_definition_concepts": 0,
            "candidate_definition_concepts": 0,
            "human_reviewed_concepts": 0,
            "human_reviewed_definition_concepts": 0,
            "model_preprocessed_concepts": 0,
            "label_candidate_concepts": 0,
            "shortened_label_candidate_concepts": 0,
            "unchanged_label_candidate_concepts": 0,
            "collision_label_candidate_concepts": 0,
            "generic_label_candidate_concepts": 0,
            "quality_review_label_candidate_concepts": 0,
            "provenance_missing_label_candidate_concepts": 0,
            "missing_label_candidate_concepts": 0,
            "human_reviewed_label_concepts": 0,
            "llm_reviewed_label_concepts": 0,
            "llm_or_human_reviewed_label_candidate_concepts": 0,
            "human_confirmed_label_candidate_anomalies": 0,
            "concepts_with_meaning_candidates": 0,
            "task_context_evidence_concepts": 0,
            "criteria_evidence_linked_ksa": 0,
            "atomic_preprocessed_ksa": 0,
            "atomic_concept_linked_ksa": 0,
        },
        "definition_status_counts": {},
        "concept_type_counts": {},
        "concept_review_status_counts": {},
        "label_review_status_counts": {},
        "meaning_review_status_counts": {},
        "label_review_progress": _label_review_progress_from_counts({}),
        "meaning_review_progress": _label_review_progress_from_counts(
            {},
            unit="filtered_term_definition_candidates",
            unit_label="Filtered term definition candidates",
        ),
        "label_quality_flag_counts": {},
        "items": [],
    }


def ksa_definition_dashboard_scope_required_payload(
    params: dict[str, list[str]],
    limit: int,
) -> dict:
    zero_summary = {
        "matching_ksa": 0,
        "linked_ksa": 0,
        "unlinked_ksa": 0,
        "concepts": 0,
        "concepts_with_definition_text": 0,
        "defined_concepts": 0,
        "missing_definition_concepts": 0,
        "candidate_definition_concepts": 0,
        "human_reviewed_concepts": 0,
        "human_reviewed_definition_concepts": 0,
        "model_preprocessed_concepts": 0,
        "label_candidate_concepts": 0,
        "shortened_label_candidate_concepts": 0,
        "unchanged_label_candidate_concepts": 0,
        "collision_label_candidate_concepts": 0,
        "generic_label_candidate_concepts": 0,
        "quality_review_label_candidate_concepts": None,
        "provenance_missing_label_candidate_concepts": 0,
        "missing_label_candidate_concepts": 0,
        "human_reviewed_label_concepts": 0,
        "llm_reviewed_label_concepts": 0,
        "llm_or_human_reviewed_label_candidate_concepts": 0,
        "human_confirmed_label_candidate_anomalies": 0,
        "concepts_with_meaning_candidates": 0,
        "task_context_evidence_concepts": 0,
        "criteria_evidence_linked_ksa": 0,
        "atomic_preprocessed_ksa": 0,
        "atomic_concept_linked_ksa": 0,
    }
    return {
        "ok": False,
        "schema": "ncs_ksa_definition_dashboard_v1",
        "generated_at": now_utc(),
        "limit": limit,
        "filters": {
            "major_code": first(params, "major_code"),
            "middle_code": first(params, "middle_code"),
            "small_code": first(params, "small_code"),
            "sub_code": first(params, "sub_code"),
            "keyword": first(params, "keyword"),
            "concept_type": first(params, "concept_type") or "all",
            "definition_state": first(params, "definition_state", "all"),
            "label_state": first(params, "label_state", "all"),
            "label_review_status": first(params, "label_review_status", "all"),
            "meaning_review_status": first(params, "meaning_review_status", "all"),
        },
        "error": {
            "code": "scope_required",
            "detail": (
                "Label State collision/quality_review and Label Review Status filters require a scope filter "
                "such as Major, Middle, Small, Sub, Keyword, Concept Type, or Definition State."
            ),
        },
        "summary": zero_summary,
        "definition_status_counts": {},
        "concept_type_counts": {},
        "concept_review_status_counts": {},
        "label_review_status_counts": {},
        "meaning_review_status_counts": {},
        "label_review_progress": _label_review_progress_from_counts({}),
        "meaning_review_progress": _label_review_progress_from_counts(
            {},
            unit="filtered_term_definition_candidates",
            unit_label="Filtered term definition candidates",
        ),
        "label_quality_flag_counts": {},
        "items": [],
    }


def ksa_definition_filter_sql(params: dict[str, list[str]]) -> tuple[str, list[object]]:
    clauses, values = classification_filters(params, "c")
    keyword = first(params, "keyword")
    if keyword:
        clauses.append(
            """
            (
              ki.ksa_text_raw LIKE ?
              OR COALESCE(ki.ksa_text_refined, '') LIKE ?
              OR COALESCE(oc.concept_name, '') LIKE ?
              OR COALESCE(oc.definition, '') LIKE ?
              OR cu.unit_code LIKE ?
              OR cu.unit_name_raw LIKE ?
              OR ce.element_name_raw LIKE ?
              OR EXISTS (
                SELECT 1
                FROM ksa_atomic_items atom
                WHERE atom.ksa_id = ki.ksa_id
                  AND atom.atom_text LIKE ?
              )
              OR {label_exists}
            )
            """
            .format(label_exists=ksa_definition_label_exists_sql("label.label_text LIKE ?"))
        )
        values.extend([f"%{keyword}%"] * 9)

    concept_type = first(params, "concept_type")
    if concept_type in {"knowledge", "skill", "attitude"}:
        clauses.append("oc.concept_type = ?")
        values.append(concept_type)

    definition_state = first(params, "definition_state", "all")
    if definition_state == "defined":
        clauses.append(
            "oc.definition_status = 'defined' AND oc.definition IS NOT NULL AND TRIM(oc.definition) <> ''"
        )
    elif definition_state == "missing":
        clauses.append(
            "oc.concept_id IS NOT NULL AND (oc.definition IS NULL OR TRIM(oc.definition) = '')"
        )
    elif definition_state == "candidate":
        clauses.append("oc.definition_status = 'candidate'")
    elif definition_state == "human_reviewed":
        clauses.append("oc.review_status = 'human_reviewed'")
    elif definition_state == "unlinked":
        clauses.append("oc.concept_id IS NULL")

    label_state = first(params, "label_state", "all")
    if label_state in {"shortened", "unchanged", "generic"}:
        clauses.append(ksa_definition_label_exists_sql(ksa_definition_label_state_condition(label_state)))
    elif label_state == "quality_review" and not ksa_definition_label_state_uses_concept_prefilter(params):
        clauses.append(ksa_definition_label_exists_sql(ksa_definition_label_state_condition(label_state)))
    elif label_state == "missing":
        clauses.append(f"oc.concept_id IS NOT NULL AND NOT {ksa_definition_label_exists_sql('1 = 1')}")

    label_review_status = first(params, "label_review_status", "all")
    label_review_condition = ksa_definition_label_review_status_condition(label_review_status)
    if label_review_condition == "__missing__":
        clauses.append(f"oc.concept_id IS NOT NULL AND NOT {ksa_definition_label_exists_sql('1 = 1')}")
    elif label_review_condition != "1 = 1":
        clauses.append(ksa_definition_label_exists_sql(label_review_condition))

    meaning_review_status = first(params, "meaning_review_status", "all")
    meaning_review_condition = ksa_definition_meaning_review_status_condition(meaning_review_status)
    if meaning_review_condition == "__missing__":
        clauses.append(f"oc.concept_id IS NOT NULL AND NOT {ksa_definition_meaning_exists_sql('1 = 1')}")
    elif meaning_review_condition != "1 = 1":
        clauses.append(ksa_definition_meaning_exists_sql(meaning_review_condition))

    return (f"WHERE {' AND '.join(clauses)}" if clauses else "", values)


KSA_DEFINITION_BASE_FROM = """
FROM ksa_items ki
JOIN competency_elements ce ON ce.element_id = ki.element_id
JOIN competency_units cu ON cu.unit_code = ce.unit_code
JOIN classifications c ON c.classification_id = cu.classification_id
LEFT JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
LEFT JOIN ontology_concepts oc ON oc.concept_id = kcl.concept_id
"""

KSA_DEFINITION_CURRENT_SCOPE_KEY_SQL = """
                  c.major_code || ':' || c.middle_code || ':' ||
                  c.small_code || ':' || c.sub_code
"""

KSA_DEFINITION_LABEL_FILTER_TABLE = "temp_ksa_definition_label_filter"
KSA_DEFINITION_KSA_FILTER_TABLE = "temp_ksa_definition_ksa_filter"

KSA_DEFINITION_LABEL_SOURCE_SCOPE_SQL = """
                AND (
                  label.source_scope_key = (
                    c.major_code || ':' || c.middle_code || ':' ||
                    c.small_code || ':' || c.sub_code
                  )
                  OR (
                    COALESCE(NULLIF(label.source_scope_key, ''), 'unknown') = 'unknown'
                    AND (
                      EXISTS (
                        SELECT 1
                        FROM ksa_items label_ki
                        JOIN competency_elements label_ce ON label_ce.element_id = label_ki.element_id
                        JOIN competency_units label_cu ON label_cu.unit_code = label_ce.unit_code
                        JOIN classifications label_c ON label_c.classification_id = label_cu.classification_id
                        WHERE label_ki.ksa_id = label.source_ksa_id
                          AND label_c.major_code = c.major_code
                          AND label_c.middle_code = c.middle_code
                          AND label_c.small_code = c.small_code
                          AND label_c.sub_code = c.sub_code
                      )
                      OR EXISTS (
                        SELECT 1
                        FROM ksa_atomic_items label_atom
                        JOIN competency_elements label_ce ON label_ce.element_id = label_atom.element_id
                        JOIN competency_units label_cu ON label_cu.unit_code = label_ce.unit_code
                        JOIN classifications label_c ON label_c.classification_id = label_cu.classification_id
                        WHERE label_atom.atomic_id = label.source_atomic_id
                          AND label_c.major_code = c.major_code
                          AND label_c.middle_code = c.middle_code
                          AND label_c.small_code = c.small_code
                          AND label_c.sub_code = c.sub_code
                      )
                    )
                  )
                )
"""

KSA_DEFINITION_LABEL_CURRENT_KSA_SQL = """
                AND (
                  label.source_ksa_id = ki.ksa_id
                  OR EXISTS (
                    SELECT 1
                    FROM ksa_atomic_items current_label_atom
                    WHERE current_label_atom.ksa_id = ki.ksa_id
                      AND current_label_atom.atomic_id = label.source_atomic_id
                  )
                )
"""

KSA_DEFINITION_MEANING_CURRENT_KSA_SQL = """
                  AND kmc.ksa_id = ki.ksa_id
"""

KSA_DEFINITION_LABEL_DISPLAY_ORDER_SQL = """
                ORDER BY
                  label.candidate_rank,
                  label.confidence_score DESC,
                  label.label_id
"""

KSA_DEFINITION_LABEL_CURRENT_ROW_SCOPE_SQL = """
            AND (
              label.source_scope_key = current_rows.scope_key
              OR (
                COALESCE(NULLIF(label.source_scope_key, ''), 'unknown') = 'unknown'
                AND (
                  (
                    label.source_ksa_id IS NOT NULL
                    AND label_ki_c.major_code = current_rows.major_code
                    AND label_ki_c.middle_code = current_rows.middle_code
                    AND label_ki_c.small_code = current_rows.small_code
                    AND label_ki_c.sub_code = current_rows.sub_code
                  )
                  OR (
                    label.source_atomic_id IS NOT NULL
                    AND label_atom_c.major_code = current_rows.major_code
                    AND label_atom_c.middle_code = current_rows.middle_code
                    AND label_atom_c.small_code = current_rows.small_code
                    AND label_atom_c.sub_code = current_rows.sub_code
                  )
                )
              )
            )
            AND (
              label.source_ksa_id = current_rows.ksa_id
              OR EXISTS (
                SELECT 1
                FROM ksa_atomic_items current_label_atom
                WHERE current_label_atom.ksa_id = current_rows.ksa_id
                  AND current_label_atom.atomic_id = label.source_atomic_id
              )
            )
"""

KSA_DEFINITION_GENERIC_LABEL_SQL = """
                LENGTH(TRIM(label.label_text)) <= 3
                OR label.normalized_label_key IN (
                  'knowledge', 'skill', 'attitude', 'management', 'analysis',
                  'planning', 'operation', 'support', 'communication'
                )
"""

KSA_DEFINITION_UNBALANCED_PARENTHESES_LABEL_SQL = """
                (
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '(', ''))) !=
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, ')', '')))
                  OR (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '[', ''))) !=
                     (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, ']', '')))
                  OR (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '【', ''))) !=
                     (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '】', '')))
                  OR (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '「', ''))) !=
                     (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '」', '')))
                  OR (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '｢', ''))) !=
                     (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '｣', '')))
                )
"""

KSA_DEFINITION_DANGLING_ENUM_LABEL_SQL = """
                (
                  TRIM(label.label_text) LIKE '%등'
                  OR TRIM(label.label_text) LIKE '%및'
                )
"""

KSA_DEFINITION_SHORT_ACRONYM_LABEL_SQL = """
                (
                  label.label_text GLOB '*[A-Z]*'
                  AND (
                    label.label_text GLOB '[A-Z0-9][A-Z0-9]'
                    OR label.label_text GLOB '[A-Z0-9][A-Z0-9][A-Z0-9]'
                    OR label.label_text GLOB '[A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9]'
                    OR label.label_text GLOB '[A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9][A-Z0-9]'
                  )
                )
"""

KSA_DEFINITION_DIGIT_COUNT_SQL = """
                (
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '0', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '1', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '2', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '3', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '4', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '5', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '6', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '7', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '8', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '9', '')))
                )
"""

KSA_DEFINITION_DIGIT_HEAVY_LABEL_SQL = f"""
                (
                  {KSA_DEFINITION_DIGIT_COUNT_SQL} >= 3
                  AND {KSA_DEFINITION_DIGIT_COUNT_SQL} * 100 >=
                    LENGTH(REPLACE(COALESCE(label.label_text, ''), ' ', '')) * 30
                )
"""

KSA_DEFINITION_SYMBOL_COUNT_SQL = """
                (
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '/', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '-', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '_', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, ',', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '.', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, ':', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, ';', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '·', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '(', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, ')', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, '[', ''))) +
                  (LENGTH(label.label_text) - LENGTH(REPLACE(label.label_text, ']', '')))
                )
"""

KSA_DEFINITION_SYMBOL_HEAVY_LABEL_SQL = f"""
                (
                  {KSA_DEFINITION_SYMBOL_COUNT_SQL} > 0
                  AND {KSA_DEFINITION_SYMBOL_COUNT_SQL} * 100 >=
                    LENGTH(REPLACE(COALESCE(label.label_text, ''), ' ', '')) * 35
                )
"""

KSA_DEFINITION_VERY_LOW_LABEL_SOURCE_RATIO_SQL = """
                (
                  COALESCE(label.source_text, '') <> ''
                  AND label.label_text <> label.source_text
                  AND LENGTH(label.label_text) * 100 < LENGTH(label.source_text) * 15
                )
"""

KSA_DEFINITION_CHANGED_NEAR_FULL_LENGTH_SQL = """
                (
                  COALESCE(label.source_text, '') <> ''
                  AND label.label_text <> label.source_text
                  AND LENGTH(label.label_text) * 100 >= LENGTH(label.source_text) * 90
                )
"""

KSA_DEFINITION_QUALITY_REVIEW_LABEL_SQL = f"""
                {KSA_DEFINITION_GENERIC_LABEL_SQL}
                OR {KSA_DEFINITION_DANGLING_ENUM_LABEL_SQL}
                OR {KSA_DEFINITION_UNBALANCED_PARENTHESES_LABEL_SQL}
                OR {KSA_DEFINITION_SHORT_ACRONYM_LABEL_SQL}
                OR {KSA_DEFINITION_DIGIT_HEAVY_LABEL_SQL}
                OR {KSA_DEFINITION_SYMBOL_HEAVY_LABEL_SQL}
                OR {KSA_DEFINITION_VERY_LOW_LABEL_SOURCE_RATIO_SQL}
                OR {KSA_DEFINITION_CHANGED_NEAR_FULL_LENGTH_SQL}
"""

KSA_DEFINITION_QUALITY_FLAG_SQLS = {
    "generic_or_low_specificity": KSA_DEFINITION_GENERIC_LABEL_SQL,
    "dangling_enum_suffix": KSA_DEFINITION_DANGLING_ENUM_LABEL_SQL,
    "unbalanced_parentheses": KSA_DEFINITION_UNBALANCED_PARENTHESES_LABEL_SQL,
    "short_acronym_needs_context": KSA_DEFINITION_SHORT_ACRONYM_LABEL_SQL,
    "digit_heavy": KSA_DEFINITION_DIGIT_HEAVY_LABEL_SQL,
    "symbol_heavy": KSA_DEFINITION_SYMBOL_HEAVY_LABEL_SQL,
    "very_low_label_source_ratio": KSA_DEFINITION_VERY_LOW_LABEL_SOURCE_RATIO_SQL,
    "changed_near_full_length": KSA_DEFINITION_CHANGED_NEAR_FULL_LENGTH_SQL,
}

KSA_DEFINITION_COLLISION_LABEL_SQL = """
                label.normalized_label_key IN (
                    SELECT collision.normalized_label_key
                    FROM ontology_concept_label_candidates collision
                    GROUP BY collision.normalized_label_key
                    HAVING COUNT(DISTINCT collision.concept_id) > 1
                )
"""

KSA_DEFINITION_SCOPED_COLLISION_LABEL_SQL = """
                (
                  (
                    COALESCE(NULLIF(label.source_scope_key, ''), 'unknown') != 'unknown'
                    AND label.normalized_label_key IN (
                      SELECT collision.normalized_label_key
                      FROM ontology_concept_label_candidates collision
                      WHERE collision.source_scope_key = label.source_scope_key
                      GROUP BY collision.normalized_label_key
                      HAVING COUNT(DISTINCT collision.concept_id) > 1
                    )
                  )
                  OR (
                    COALESCE(NULLIF(label.source_scope_key, ''), 'unknown') = 'unknown'
                    AND label.normalized_label_key IN (
                      SELECT collision.normalized_label_key
                      FROM ontology_concept_label_candidates collision
                      WHERE collision.normalized_label_key = label.normalized_label_key
                        AND (
                          EXISTS (
                            SELECT 1
                            FROM ksa_items collision_ki
                            JOIN competency_elements collision_ce ON collision_ce.element_id = collision_ki.element_id
                            JOIN competency_units collision_cu ON collision_cu.unit_code = collision_ce.unit_code
                            JOIN classifications collision_c ON collision_c.classification_id = collision_cu.classification_id
                            WHERE collision_ki.ksa_id = collision.source_ksa_id
                              AND collision_c.major_code = c.major_code
                              AND collision_c.middle_code = c.middle_code
                              AND collision_c.small_code = c.small_code
                              AND collision_c.sub_code = c.sub_code
                          )
                          OR EXISTS (
                            SELECT 1
                            FROM ksa_atomic_items collision_atom
                            JOIN competency_elements collision_ce ON collision_ce.element_id = collision_atom.element_id
                            JOIN competency_units collision_cu ON collision_cu.unit_code = collision_ce.unit_code
                            JOIN classifications collision_c ON collision_c.classification_id = collision_cu.classification_id
                            WHERE collision_atom.atomic_id = collision.source_atomic_id
                              AND collision_c.major_code = c.major_code
                              AND collision_c.middle_code = c.middle_code
                              AND collision_c.small_code = c.small_code
                              AND collision_c.sub_code = c.sub_code
                          )
                        )
                      GROUP BY collision.normalized_label_key
                      HAVING COUNT(DISTINCT collision.concept_id) > 1
                    )
                  )
                )
"""


def ksa_definition_label_exists_sql(condition: str) -> str:
    return f"""
            EXISTS (
              SELECT 1
              FROM ontology_concept_label_candidates label
              WHERE label.concept_id = oc.concept_id
              {KSA_DEFINITION_LABEL_SOURCE_SCOPE_SQL}
              {KSA_DEFINITION_LABEL_CURRENT_KSA_SQL}
                AND ({condition})
            )
            """


def ksa_definition_meaning_exists_sql(condition: str) -> str:
    return f"""
            EXISTS (
              SELECT 1
              FROM ksa_meaning_candidates kmc
              WHERE kmc.concept_id = oc.concept_id
                {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
                AND ({condition})
            )
            """


def ksa_definition_label_state_condition(label_state: str) -> str:
    if label_state == "shortened":
        return "label.source_method = 'rule_based_short_label_candidate'"
    if label_state == "unchanged":
        return "label.source_method = 'already_short_label'"
    if label_state == "collision":
        return KSA_DEFINITION_SCOPED_COLLISION_LABEL_SQL
    if label_state == "generic":
        return KSA_DEFINITION_GENERIC_LABEL_SQL
    if label_state == "quality_review":
        return KSA_DEFINITION_QUALITY_REVIEW_LABEL_SQL
    return "1 = 1"


def ksa_definition_label_review_status_condition(label_review_status: str) -> str:
    allowed = {
        "candidate",
        "human_reviewed",
        "llm_reviewed",
        "needs_review",
        "rejected",
        "accepted",
        "reviewed",
    }
    if label_review_status in allowed:
        return f"label.review_status = '{label_review_status}'"
    if label_review_status == "missing":
        return "__missing__"
    return "1 = 1"


def ksa_definition_meaning_review_status_condition(meaning_review_status: str) -> str:
    allowed = {
        "candidate",
        "human_reviewed",
        "llm_reviewed",
        "needs_review",
        "rejected",
        "accepted",
        "reviewed",
    }
    if meaning_review_status in allowed:
        return f"kmc.review_status = '{meaning_review_status}'"
    if meaning_review_status == "missing":
        return "__missing__"
    return "1 = 1"


def _grouped_count_map(
    conn: sqlite3.Connection,
    expression: str,
    where: str,
    values: list[object],
) -> dict[str, int]:
    expression = expression.replace(
        "{KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}",
        KSA_DEFINITION_MEANING_CURRENT_KSA_SQL,
    )
    rows = conn.execute(
        f"""
        SELECT {expression} AS key, COUNT(*) AS count
        {KSA_DEFINITION_BASE_FROM}
        {where}
        GROUP BY 1
        ORDER BY count DESC, key
        """,
        values,
    ).fetchall()
    return {str(row["key"]): int(row["count"]) for row in rows}


def _label_review_human_action_count_map(
    conn: sqlite3.Connection,
    where: str,
    values: list[object],
    label_condition: str,
) -> dict[str, int]:
    if not table_exists(conn, "review_audit_log"):
        return {}
    blocked_reviewers = tuple(sorted(DASHBOARD_AUTOMATED_REVIEWER_IDS))
    blocked_placeholders = ", ".join("?" for _ in blocked_reviewers)
    rows = conn.execute(
        f"""
        SELECT COALESCE(action, 'none') AS key, COUNT(*) AS count
        FROM (
          SELECT (
            SELECT audit.action
            FROM review_audit_log audit
            WHERE audit.entity_type = 'ontology_concept_label_candidate'
              AND audit.entity_id = CAST(scoped_labels.label_id AS TEXT)
              AND TRIM(COALESCE(audit.reviewer_id, '')) <> ''
              AND LOWER(TRIM(audit.reviewer_id)) NOT IN ({blocked_placeholders})
            ORDER BY audit.id DESC
            LIMIT 1
          ) AS action
          FROM (
            SELECT (
              SELECT label.label_id
              FROM ontology_concept_label_candidates label
              WHERE label.concept_id = oc.concept_id
              {KSA_DEFINITION_LABEL_SOURCE_SCOPE_SQL}
              {KSA_DEFINITION_LABEL_CURRENT_KSA_SQL}
                AND ({label_condition})
              {KSA_DEFINITION_LABEL_DISPLAY_ORDER_SQL}
              LIMIT 1
            ) AS label_id
            {KSA_DEFINITION_BASE_FROM}
            {where}
          ) scoped_labels
          WHERE scoped_labels.label_id IS NOT NULL
        )
        GROUP BY 1
        ORDER BY count DESC, key
        """,
        [*blocked_reviewers, *values],
    ).fetchall()
    return {str(row["key"]): int(row["count"] or 0) for row in rows}


def _label_quality_flag_count_map(
    conn: sqlite3.Connection,
    where: str,
    values: list[object],
) -> dict[str, int]:
    select_columns = [
        f"""
        COUNT(DISTINCT CASE
          WHEN {ksa_definition_label_exists_sql(condition)}
          THEN oc.concept_id END
        ) AS {flag}
        """
        for flag, condition in KSA_DEFINITION_QUALITY_FLAG_SQLS.items()
    ]
    row = conn.execute(
        f"""
        SELECT {", ".join(select_columns)}
        {KSA_DEFINITION_BASE_FROM}
        {where}
        """,
        values,
    ).fetchone()
    return {
        flag: int(row[flag] or 0)
        for flag in KSA_DEFINITION_QUALITY_FLAG_SQLS
    }


def _label_condition_concept_count(
    conn: sqlite3.Connection,
    where: str,
    values: list[object],
    condition: str,
) -> int:
    row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT CASE
          WHEN {ksa_definition_label_exists_sql(condition)}
          THEN oc.concept_id END
        ) AS count
        {KSA_DEFINITION_BASE_FROM}
        {where}
        """,
        values,
    ).fetchone()
    return int(row["count"] or 0)


def _reset_label_filter_table(conn: sqlite3.Connection) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {KSA_DEFINITION_LABEL_FILTER_TABLE}")
    conn.execute(
        f"""
        CREATE TEMP TABLE {KSA_DEFINITION_LABEL_FILTER_TABLE} (
          ksa_id INTEGER NOT NULL,
          label_id INTEGER NOT NULL,
          concept_id INTEGER NOT NULL,
          scope_key TEXT NOT NULL,
          PRIMARY KEY (ksa_id, label_id, concept_id, scope_key)
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX idx_{KSA_DEFINITION_LABEL_FILTER_TABLE}_ksa_concept_scope
        ON {KSA_DEFINITION_LABEL_FILTER_TABLE}(ksa_id, concept_id, scope_key)
        """
    )


def _reset_ksa_filter_table(conn: sqlite3.Connection) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {KSA_DEFINITION_KSA_FILTER_TABLE}")
    conn.execute(
        f"""
        CREATE TEMP TABLE {KSA_DEFINITION_KSA_FILTER_TABLE} (
          ksa_id INTEGER PRIMARY KEY
        )
        """
    )


def _append_ksa_filter_where(where: str) -> str:
    ksa_filter = f"""
        ki.ksa_id IN (
          SELECT ksa_id
          FROM {KSA_DEFINITION_KSA_FILTER_TABLE}
        )
    """
    return f"{where} AND {ksa_filter}" if where else f"WHERE {ksa_filter}"


def _populate_ksa_filter_table(
    conn: sqlite3.Connection,
    where: str,
    values: list[object],
) -> int:
    _reset_ksa_filter_table(conn)
    conn.execute(
        f"""
        INSERT OR IGNORE INTO {KSA_DEFINITION_KSA_FILTER_TABLE}(ksa_id)
        SELECT DISTINCT ki.ksa_id
        {KSA_DEFINITION_BASE_FROM}
        {where}
        """,
        values,
    )
    return _dashboard_scalar_int(
        conn,
        f"SELECT COUNT(*) FROM {KSA_DEFINITION_KSA_FILTER_TABLE}",
    )


def _append_label_filter_scope_where(where: str) -> str:
    label_filter = f"""
        EXISTS (
          SELECT 1
          FROM {KSA_DEFINITION_LABEL_FILTER_TABLE} active_label_filter
          WHERE active_label_filter.concept_id = oc.concept_id
            AND active_label_filter.ksa_id = ki.ksa_id
            AND active_label_filter.scope_key = ({KSA_DEFINITION_CURRENT_SCOPE_KEY_SQL})
        )
    """
    return f"{where} AND {label_filter}" if where else f"WHERE {label_filter}"


def _scoped_label_condition_filter_insert_sql(condition: str) -> str:
    return f"""
        INSERT OR IGNORE INTO {KSA_DEFINITION_LABEL_FILTER_TABLE}
          (ksa_id, label_id, concept_id, scope_key)
        WITH current_rows AS (
          SELECT DISTINCT
            ki.ksa_id,
            oc.concept_id,
            c.major_code,
            c.middle_code,
            c.small_code,
            c.sub_code,
            c.major_code || ':' || c.middle_code || ':' ||
              c.small_code || ':' || c.sub_code AS scope_key
          {KSA_DEFINITION_BASE_FROM}
          {{current_where}}
        )
        SELECT DISTINCT
          current_rows.ksa_id,
          label.label_id,
          current_rows.concept_id,
          current_rows.scope_key
        FROM current_rows
        JOIN ontology_concept_label_candidates label
          ON label.concept_id = current_rows.concept_id
        LEFT JOIN ksa_items label_ki
          ON label_ki.ksa_id = label.source_ksa_id
        LEFT JOIN competency_elements label_ki_ce
          ON label_ki_ce.element_id = label_ki.element_id
        LEFT JOIN competency_units label_ki_cu
          ON label_ki_cu.unit_code = label_ki_ce.unit_code
        LEFT JOIN classifications label_ki_c
          ON label_ki_c.classification_id = label_ki_cu.classification_id
        LEFT JOIN ksa_atomic_items label_atom
          ON label_atom.atomic_id = label.source_atomic_id
        LEFT JOIN competency_elements label_atom_ce
          ON label_atom_ce.element_id = label_atom.element_id
        LEFT JOIN competency_units label_atom_cu
          ON label_atom_cu.unit_code = label_atom_ce.unit_code
        LEFT JOIN classifications label_atom_c
          ON label_atom_c.classification_id = label_atom_cu.classification_id
        WHERE ({condition})
          {KSA_DEFINITION_LABEL_CURRENT_ROW_SCOPE_SQL}
    """


def _populate_scoped_label_condition_filter(
    conn: sqlite3.Connection,
    where: str,
    values: list[object],
    condition: str,
) -> int:
    current_where = (
        f"{where} AND oc.concept_id IS NOT NULL"
        if where
        else "WHERE oc.concept_id IS NOT NULL"
    )
    sql = _scoped_label_condition_filter_insert_sql(condition).format(
        current_where=current_where
    )
    conn.execute(sql, values)
    return _dashboard_scalar_int(
        conn,
        f"SELECT COUNT(*) FROM {KSA_DEFINITION_LABEL_FILTER_TABLE}",
    )


def _populate_scoped_collision_label_filter(
    conn: sqlite3.Connection,
    where: str,
    values: list[object],
) -> int:
    current_where = (
        f"{where} AND oc.concept_id IS NOT NULL"
        if where
        else "WHERE oc.concept_id IS NOT NULL"
    )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO {KSA_DEFINITION_LABEL_FILTER_TABLE}
          (ksa_id, label_id, concept_id, scope_key)
        WITH current_rows AS (
          SELECT DISTINCT
            ki.ksa_id,
            oc.concept_id,
            c.major_code,
            c.middle_code,
            c.small_code,
            c.sub_code,
            c.major_code || ':' || c.middle_code || ':' ||
              c.small_code || ':' || c.sub_code AS scope_key
          {KSA_DEFINITION_BASE_FROM}
          {current_where}
        ),
        scoped_labels AS (
          SELECT DISTINCT
            current_rows.ksa_id,
            label.label_id,
            current_rows.concept_id,
            current_rows.scope_key,
            label.normalized_label_key
          FROM current_rows
          JOIN ontology_concept_label_candidates label
            ON label.concept_id = current_rows.concept_id
          LEFT JOIN ksa_items label_ki
            ON label_ki.ksa_id = label.source_ksa_id
          LEFT JOIN competency_elements label_ki_ce
            ON label_ki_ce.element_id = label_ki.element_id
          LEFT JOIN competency_units label_ki_cu
            ON label_ki_cu.unit_code = label_ki_ce.unit_code
          LEFT JOIN classifications label_ki_c
            ON label_ki_c.classification_id = label_ki_cu.classification_id
          LEFT JOIN ksa_atomic_items label_atom
            ON label_atom.atomic_id = label.source_atomic_id
          LEFT JOIN competency_elements label_atom_ce
            ON label_atom_ce.element_id = label_atom.element_id
          LEFT JOIN competency_units label_atom_cu
            ON label_atom_cu.unit_code = label_atom_ce.unit_code
          LEFT JOIN classifications label_atom_c
            ON label_atom_c.classification_id = label_atom_cu.classification_id
          WHERE label.normalized_label_key IS NOT NULL
            {KSA_DEFINITION_LABEL_CURRENT_ROW_SCOPE_SQL}
        )
        SELECT ksa_id, label_id, concept_id, scope_key
        FROM scoped_labels
        WHERE normalized_label_key IN (
          SELECT normalized_label_key
          FROM scoped_labels collision
          WHERE collision.scope_key = scoped_labels.scope_key
          GROUP BY normalized_label_key
          HAVING COUNT(DISTINCT concept_id) > 1
        )
        """,
        values,
    )
    return _dashboard_scalar_int(
        conn,
        f"SELECT COUNT(*) FROM {KSA_DEFINITION_LABEL_FILTER_TABLE}",
    )


def _scoped_collision_label_concept_count(
    conn: sqlite3.Connection,
    where: str,
    values: list[object],
) -> int:
    return len(_scoped_collision_label_concept_ids(conn, where, values))


def _scoped_collision_label_concept_ids(
    conn: sqlite3.Connection,
    where: str,
    values: list[object],
) -> list[int]:
    current_where = (
        f"{where} AND oc.concept_id IS NOT NULL"
        if where
        else "WHERE oc.concept_id IS NOT NULL"
    )
    rows = conn.execute(
        f"""
        WITH current_rows AS (
          SELECT DISTINCT
            oc.concept_id,
            c.major_code,
            c.middle_code,
            c.small_code,
            c.sub_code
          {KSA_DEFINITION_BASE_FROM}
          {current_where}
        ),
        scoped_labels AS (
          SELECT DISTINCT
            current_rows.concept_id,
            label.normalized_label_key
          FROM current_rows
          JOIN ontology_concept_label_candidates label
            ON label.concept_id = current_rows.concept_id
          LEFT JOIN ksa_items label_ki
            ON label_ki.ksa_id = label.source_ksa_id
          LEFT JOIN competency_elements label_ki_ce
            ON label_ki_ce.element_id = label_ki.element_id
          LEFT JOIN competency_units label_ki_cu
            ON label_ki_cu.unit_code = label_ki_ce.unit_code
          LEFT JOIN classifications label_ki_c
            ON label_ki_c.classification_id = label_ki_cu.classification_id
          LEFT JOIN ksa_atomic_items label_atom
            ON label_atom.atomic_id = label.source_atomic_id
          LEFT JOIN competency_elements label_atom_ce
            ON label_atom_ce.element_id = label_atom.element_id
          LEFT JOIN competency_units label_atom_cu
            ON label_atom_cu.unit_code = label_atom_ce.unit_code
          LEFT JOIN classifications label_atom_c
            ON label_atom_c.classification_id = label_atom_cu.classification_id
          WHERE label.normalized_label_key IS NOT NULL
            AND (
              (
                label.source_ksa_id IS NOT NULL
                AND label_ki_c.major_code = current_rows.major_code
                AND label_ki_c.middle_code = current_rows.middle_code
                AND label_ki_c.small_code = current_rows.small_code
                AND label_ki_c.sub_code = current_rows.sub_code
              )
              OR (
                label.source_atomic_id IS NOT NULL
                AND label_atom_c.major_code = current_rows.major_code
                AND label_atom_c.middle_code = current_rows.middle_code
                AND label_atom_c.small_code = current_rows.small_code
                AND label_atom_c.sub_code = current_rows.sub_code
              )
            )
        )
        SELECT DISTINCT concept_id
        FROM scoped_labels
        WHERE normalized_label_key IN (
          SELECT normalized_label_key
          FROM scoped_labels
          GROUP BY normalized_label_key
          HAVING COUNT(DISTINCT concept_id) > 1
        )
        """,
        values,
    ).fetchall()
    return [int(row["concept_id"]) for row in rows]


def ksa_definition_has_non_label_scope(params: dict[str, list[str]]) -> bool:
    for key in (
        "major_code",
        "middle_code",
        "small_code",
        "sub_code",
        "keyword",
        "concept_type",
        "definition_state",
    ):
        value = first(params, key)
        if value and value != "all":
            return True
    return False


def ksa_definition_has_review_queue_scope(params: dict[str, list[str]]) -> bool:
    for key in ("major_code", "middle_code", "small_code", "sub_code", "keyword"):
        value = first(params, key)
        if value and value != "all":
            return True
    return False


def ksa_definition_label_state_uses_concept_prefilter(params: dict[str, list[str]]) -> bool:
    label_state = first(params, "label_state", "all")
    if label_state in {"collision", "quality_review"}:
        return ksa_definition_has_non_label_scope(params)
    return False


def ksa_definition_label_state_requires_scope(params: dict[str, list[str]]) -> bool:
    label_state = first(params, "label_state", "all")
    label_review_condition = ksa_definition_label_review_status_condition(
        first(params, "label_review_status", "all")
    )
    label_review_filter_active = label_review_condition != "1 = 1"
    if label_review_filter_active and not ksa_definition_has_review_queue_scope(params):
        return True
    return label_state in {"collision", "quality_review"} and not ksa_definition_has_non_label_scope(params)


def ksa_definition_keyword_prefilter_enabled(params: dict[str, list[str]]) -> bool:
    if not first(params, "keyword"):
        return False
    for key in ("major_code", "middle_code", "small_code", "sub_code"):
        value = first(params, key)
        if value and value != "all":
            return False
    return True


def ksa_definition_quality_summary_enabled(params: dict[str, list[str]]) -> bool:
    for key in (
        "major_code",
        "middle_code",
        "small_code",
        "sub_code",
        "keyword",
        "concept_type",
        "definition_state",
        "label_state",
        "label_review_status",
        "meaning_review_status",
    ):
        value = first(params, key)
        if value and value != "all":
            return True
    return False


def _dashboard_scalar_int(
    conn: sqlite3.Connection,
    sql: str,
    values: tuple[object, ...] = (),
) -> int:
    row = conn.execute(sql, values).fetchone()
    return int((row[0] if row else 0) or 0)


def _dashboard_clip(value: object, limit: int) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _dashboard_task_evidence(conn: sqlite3.Connection, concept_id: object) -> dict[str, object]:
    if concept_id is None:
        return {
            "task_evidence_count": 0,
            "task_evidence_preview": [],
            "task_evidence_refs": [],
            "criteria_ids": [],
            "criteria_text_preview": [],
        }
    concept_id_int = int(concept_id)
    has_performance_criteria = table_exists(conn, "performance_criteria")
    has_task_relations = table_exists(conn, "task_ksa_concept_relations")
    pc_join_kmc = (
        "LEFT JOIN performance_criteria pc ON pc.criteria_id = kmc.criteria_id"
        if has_performance_criteria
        else ""
    )
    pc_join_ccl = (
        "LEFT JOIN performance_criteria pc ON pc.criteria_id = ccl.criteria_id"
        if has_performance_criteria
        else ""
    )
    pc_join_rel = (
        "LEFT JOIN performance_criteria pc ON pc.criteria_id = rel.criteria_id"
        if has_performance_criteria
        else ""
    )
    criteria_text_sql = "pc.criteria_text_raw" if has_performance_criteria else "NULL"
    evidence_parts = [
        f"""
          SELECT
            'ksa_meaning_candidates.' || kmc.source_method AS evidence_ref,
            kmc.criteria_id,
            {criteria_text_sql} AS criteria_text,
            kmc.evidence_text,
            kmc.meaning_text,
            kmc.review_status,
            kmc.confidence_score
          FROM ksa_meaning_candidates kmc
          {pc_join_kmc}
          WHERE kmc.concept_id = ?
            AND kmc.source_method != 'term_definition_template'
        """,
        f"""
          SELECT
            'criteria_concept_links.' || ccl.relation_type AS evidence_ref,
            ccl.criteria_id,
            {criteria_text_sql} AS criteria_text,
            NULL AS evidence_text,
            NULL AS meaning_text,
            ccl.link_status AS review_status,
            NULL AS confidence_score
          FROM criteria_concept_links ccl
          {pc_join_ccl}
          WHERE ccl.concept_id = ?
        """,
    ]
    evidence_values: list[object] = [concept_id_int, concept_id_int]
    if has_task_relations:
        evidence_parts.append(
            f"""
          SELECT
            'task_ksa_concept_relations.' || rel.relation_type AS evidence_ref,
            rel.criteria_id,
            {criteria_text_sql} AS criteria_text,
            rel.evidence_text,
            NULL AS meaning_text,
            rel.review_status,
            rel.confidence_score
          FROM task_ksa_concept_relations rel
          {pc_join_rel}
          WHERE rel.source_concept_id = ?
             OR rel.target_concept_id = ?
        """
        )
        evidence_values.extend([concept_id_int, concept_id_int])
    evidence_sql = "WITH evidence AS (\n" + "\nUNION ALL\n".join(evidence_parts) + "\n)"
    count_row = conn.execute(
        f"{evidence_sql} SELECT COUNT(*) FROM evidence",
        tuple(evidence_values),
    ).fetchone()
    rows = conn.execute(
        f"""
        {evidence_sql}
        SELECT *
        FROM evidence
        ORDER BY
          CASE WHEN criteria_id IS NULL THEN 1 ELSE 0 END,
          COALESCE(confidence_score, 0) DESC,
          evidence_ref,
          criteria_id
        LIMIT 20
        """,
        tuple(evidence_values),
    ).fetchall()
    criteria_ids: list[int] = []
    criteria_texts: list[str] = []
    previews: list[str] = []
    refs: list[str] = []
    seen_previews: set[str] = set()
    seen_refs: set[str] = set()
    for evidence in rows:
        criteria_id = evidence["criteria_id"]
        if criteria_id is not None and int(criteria_id) not in criteria_ids:
            criteria_ids.append(int(criteria_id))
        criteria_text = _dashboard_clip(evidence["criteria_text"], 140)
        if criteria_text and criteria_text not in criteria_texts:
            criteria_texts.append(criteria_text)
        ref = str(evidence["evidence_ref"] or "")
        if criteria_id is not None:
            ref = f"{ref}#criteria:{int(criteria_id)}"
        if ref not in seen_refs:
            refs.append(ref)
            seen_refs.add(ref)
        detail = (
            evidence["evidence_text"]
            or evidence["meaning_text"]
            or evidence["criteria_text"]
            or ""
        )
        preview = f"{ref}: {_dashboard_clip(detail, 180)}"
        if preview not in seen_previews:
            previews.append(preview)
            seen_previews.add(preview)
    return {
        "task_evidence_count": int((count_row[0] if count_row else 0) or 0),
        "task_evidence_preview": previews[:5],
        "task_evidence_refs": refs[:5],
        "criteria_ids": criteria_ids,
        "criteria_text_preview": criteria_texts[:3],
    }


def _dashboard_atomic_label_candidates(
    conn: sqlite3.Connection,
    ksa_id: object,
) -> list[dict[str, object]]:
    if ksa_id is None:
        return []
    rows = conn.execute(
        """
        SELECT
          atom.atom_index,
          atom.atomic_id,
          atom.atom_text,
          atom.split_method,
          atom.review_status AS atomic_review_status,
          oc.concept_id,
          oc.concept_name,
          oc.concept_type,
          label.label_id,
          label.label_text,
          label.source_method AS label_source_method,
          label.review_status AS label_review_status,
          label.confidence_score AS label_confidence
        FROM ksa_atomic_items atom
        LEFT JOIN ksa_atomic_concept_links acl ON acl.atomic_id = atom.atomic_id
        LEFT JOIN ontology_concepts oc ON oc.concept_id = acl.concept_id
        LEFT JOIN ontology_concept_label_candidates label
          ON label.concept_id = oc.concept_id
         AND (
              label.source_atomic_id = atom.atomic_id
              OR label.source_ksa_id = atom.ksa_id
         )
        WHERE atom.ksa_id = ?
        ORDER BY
          atom.atom_index,
          CASE WHEN label.source_atomic_id = atom.atomic_id THEN 0 ELSE 1 END,
          label.candidate_rank,
          label.confidence_score DESC,
          label.label_id
        LIMIT 20
        """,
        (int(ksa_id),),
    ).fetchall()
    candidates: list[dict[str, object]] = []
    seen: set[tuple[object, object]] = set()
    for row in rows:
        key = (row["atomic_id"], row["label_id"])
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "atom_index": row["atom_index"],
                "atomic_id": row["atomic_id"],
                "atom_text": row["atom_text"],
                "split_method": row["split_method"],
                "atomic_review_status": row["atomic_review_status"],
                "concept_id": row["concept_id"],
                "concept_name": row["concept_name"],
                "concept_type": row["concept_type"],
                "label_id": row["label_id"],
                "label_text": row["label_text"],
                "label_source_method": row["label_source_method"],
                "label_review_status": row["label_review_status"],
                "label_confidence": row["label_confidence"],
            }
        )
    return candidates


def _broad_ksa_definition_summary(conn: sqlite3.Connection) -> dict[str, int | None]:
    linked_concepts_sql = "SELECT DISTINCT concept_id FROM ksa_concept_links"
    label_with_source_sql = f"""
        SELECT *
        FROM ontology_concept_label_candidates
        WHERE (source_ksa_id IS NOT NULL OR source_atomic_id IS NOT NULL)
          AND concept_id IN ({linked_concepts_sql})
    """
    summary: dict[str, int | None] = {
        "matching_ksa": _dashboard_scalar_int(conn, "SELECT COUNT(*) FROM ksa_items"),
        "linked_ksa": _dashboard_scalar_int(
            conn,
            "SELECT COUNT(DISTINCT ksa_id) FROM ksa_concept_links",
        ),
        "concepts": _dashboard_scalar_int(conn, f"SELECT COUNT(*) FROM ({linked_concepts_sql})"),
        "concepts_with_definition_text": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concepts
            WHERE concept_id IN ({linked_concepts_sql})
              AND definition IS NOT NULL
              AND TRIM(definition) <> ''
            """,
        ),
        "defined_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concepts
            WHERE concept_id IN ({linked_concepts_sql})
              AND definition IS NOT NULL
              AND TRIM(definition) <> ''
              AND definition_status = 'defined'
            """,
        ),
        "missing_definition_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concepts
            WHERE concept_id IN ({linked_concepts_sql})
              AND (definition IS NULL OR TRIM(definition) = '')
            """,
        ),
        "candidate_definition_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concepts
            WHERE concept_id IN ({linked_concepts_sql})
              AND definition_status = 'candidate'
            """,
        ),
        "human_reviewed_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concepts
            WHERE concept_id IN ({linked_concepts_sql})
              AND review_status = 'human_reviewed'
            """,
        ),
        "human_reviewed_definition_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concepts
            WHERE concept_id IN ({linked_concepts_sql})
              AND review_status = 'human_reviewed'
              AND definition IS NOT NULL
              AND TRIM(definition) <> ''
            """,
        ),
        "model_preprocessed_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concepts
            WHERE concept_id IN ({linked_concepts_sql})
              AND review_status = 'model_preprocessed'
            """,
        ),
        "label_candidate_concepts": _dashboard_scalar_int(
            conn,
            f"SELECT COUNT(DISTINCT concept_id) FROM ({label_with_source_sql})",
        ),
        "shortened_label_candidate_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(DISTINCT concept_id)
            FROM ({label_with_source_sql})
            WHERE source_method = 'rule_based_short_label_candidate'
            """,
        ),
        "unchanged_label_candidate_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(DISTINCT concept_id)
            FROM ({label_with_source_sql})
            WHERE source_method = 'already_short_label'
            """,
        ),
        "collision_label_candidate_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(DISTINCT label.concept_id)
            FROM ({label_with_source_sql}) label
            WHERE label.normalized_label_key IN (
              SELECT normalized_label_key
              FROM ({label_with_source_sql})
              GROUP BY normalized_label_key
              HAVING COUNT(DISTINCT concept_id) > 1
            )
            """,
        ),
        "generic_label_candidate_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(DISTINCT concept_id)
            FROM ({label_with_source_sql}) label
            WHERE {KSA_DEFINITION_GENERIC_LABEL_SQL}
            """,
        ),
        "provenance_missing_label_candidate_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(DISTINCT concept_id)
            FROM ontology_concept_label_candidates
            WHERE source_ksa_id IS NULL
              AND source_atomic_id IS NULL
              AND concept_id IN ({linked_concepts_sql})
            """,
        ),
        "human_reviewed_label_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(DISTINCT concept_id)
            FROM ({label_with_source_sql})
            WHERE review_status = 'human_reviewed'
            """,
        ),
        "llm_reviewed_label_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(DISTINCT concept_id)
            FROM ({label_with_source_sql})
            WHERE review_status = 'llm_reviewed'
            """,
        ),
        "llm_or_human_reviewed_label_candidate_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(DISTINCT concept_id)
            FROM ({label_with_source_sql})
            WHERE review_status IN ('human_reviewed', 'accepted', 'reviewed', 'llm_reviewed')
            """,
        ),
        "human_confirmed_label_candidate_anomalies": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(DISTINCT concept_id)
            FROM ({label_with_source_sql})
            WHERE review_status IN ('human_reviewed', 'accepted', 'reviewed')
            """,
        ),
        "concepts_with_meaning_candidates": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(DISTINCT concept_id)
            FROM ksa_meaning_candidates
            WHERE concept_id IN ({linked_concepts_sql})
            """,
        ),
        "llm_reviewed_meaning_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(DISTINCT concept_id)
            FROM ksa_meaning_candidates
            WHERE concept_id IN ({linked_concepts_sql})
              AND review_status = 'llm_reviewed'
            """,
        ),
        "needs_review_meaning_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(DISTINCT concept_id)
            FROM ksa_meaning_candidates
            WHERE concept_id IN ({linked_concepts_sql})
              AND review_status = 'needs_review'
            """,
        ),
        "candidate_meaning_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(DISTINCT concept_id)
            FROM ksa_meaning_candidates
            WHERE concept_id IN ({linked_concepts_sql})
              AND review_status = 'candidate'
            """,
        ),
        "task_context_evidence_concepts": _dashboard_scalar_int(
            conn,
            f"""
            SELECT COUNT(DISTINCT concept_id)
            FROM ksa_meaning_candidates
            WHERE concept_id IN ({linked_concepts_sql})
              AND source_method != 'term_definition_template'
            """,
        ),
        "criteria_evidence_linked_ksa": _dashboard_scalar_int(
            conn,
            """
            SELECT COUNT(DISTINCT kcl.ksa_id)
            FROM ksa_concept_links kcl
            WHERE EXISTS (
              SELECT 1
              FROM criteria_concept_links ccl
              WHERE ccl.concept_id = kcl.concept_id
            )
            """,
        ),
        "atomic_preprocessed_ksa": _dashboard_scalar_int(
            conn,
            "SELECT COUNT(DISTINCT ksa_id) FROM ksa_atomic_items",
        ),
        "atomic_concept_linked_ksa": _dashboard_scalar_int(
            conn,
            """
            SELECT COUNT(DISTINCT atom.ksa_id)
            FROM ksa_atomic_items atom
            JOIN ksa_atomic_concept_links acl ON acl.atomic_id = atom.atomic_id
            """,
        ),
        "quality_review_label_candidate_concepts": None,
    }
    summary["unlinked_ksa"] = max(int(summary["matching_ksa"] or 0) - int(summary["linked_ksa"] or 0), 0)
    summary["missing_label_candidate_concepts"] = max(
        int(summary["concepts"] or 0) - int(summary["label_candidate_concepts"] or 0),
        0,
    )
    return summary


def _broad_ksa_definition_count_maps(
    conn: sqlite3.Connection,
    summary: dict[str, int | None],
) -> dict[str, dict[str, int]]:
    linked_concepts_sql = "SELECT DISTINCT concept_id FROM ksa_concept_links"

    def grouped(sql: str) -> dict[str, int]:
        rows = conn.execute(sql).fetchall()
        return {str(row["key"]): int(row["count"] or 0) for row in rows}

    label_review_status_counts = grouped(
        f"""
        WITH scoped_labels AS (
          SELECT *
          FROM ontology_concept_label_candidates
          WHERE (source_ksa_id IS NOT NULL OR source_atomic_id IS NOT NULL)
            AND concept_id IN ({linked_concepts_sql})
        ),
        representative_labels AS (
          SELECT label.concept_id, label.review_status
          FROM scoped_labels label
          WHERE NOT EXISTS (
            SELECT 1
            FROM scoped_labels better
            WHERE better.concept_id = label.concept_id
              AND (
                better.candidate_rank < label.candidate_rank
                OR (
                  better.candidate_rank = label.candidate_rank
                  AND better.confidence_score > label.confidence_score
                )
                OR (
                  better.candidate_rank = label.candidate_rank
                  AND better.confidence_score = label.confidence_score
                  AND better.label_id < label.label_id
                )
              )
          )
        )
        SELECT COALESCE(review_status, 'missing') AS key,
               COUNT(DISTINCT concept_id) AS count
        FROM representative_labels
        GROUP BY 1
        ORDER BY count DESC, key
        """
    )
    missing_labels = int(summary.get("missing_label_candidate_concepts") or 0)
    if missing_labels:
        label_review_status_counts["missing"] = missing_labels

    meaning_review_status_counts = _grouped_count_map(
        conn,
        """
        COALESCE((
          SELECT kmc.review_status
          FROM ksa_meaning_candidates kmc
          WHERE kmc.concept_id = oc.concept_id
            {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
          ORDER BY
            CASE kmc.meaning_role
              WHEN 'term_definition_candidate' THEN 0
              ELSE 1
            END,
            kmc.confidence_score DESC,
            kmc.meaning_id
          LIMIT 1
        ), 'missing')
        """,
        "",
        [],
    )

    return {
        "definition_status_counts": grouped(
            f"""
            SELECT COALESCE(definition_status, 'missing') AS key, COUNT(*) AS count
            FROM ontology_concepts
            WHERE concept_id IN ({linked_concepts_sql})
            GROUP BY 1
            ORDER BY count DESC, key
            """
        ),
        "concept_type_counts": grouped(
            f"""
            SELECT COALESCE(concept_type, 'unlinked') AS key, COUNT(*) AS count
            FROM ontology_concepts
            WHERE concept_id IN ({linked_concepts_sql})
            GROUP BY 1
            ORDER BY count DESC, key
            """
        ),
        "concept_review_status_counts": grouped(
            f"""
            SELECT COALESCE(review_status, 'unlinked') AS key, COUNT(*) AS count
            FROM ontology_concepts
            WHERE concept_id IN ({linked_concepts_sql})
            GROUP BY 1
            ORDER BY count DESC, key
            """
        ),
        "label_review_status_counts": label_review_status_counts,
        "meaning_review_status_counts": meaning_review_status_counts,
    }


def _precise_ksa_definition_summary(
    conn: sqlite3.Connection,
    where: str,
    values: list[object],
) -> dict[str, int | None]:
    def base_scalar(expression: str) -> int:
        expression = expression.replace(
            "{KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}",
            KSA_DEFINITION_MEANING_CURRENT_KSA_SQL,
        )
        return _dashboard_scalar_int(
            conn,
            f"SELECT {expression} {KSA_DEFINITION_BASE_FROM} {where}",
            tuple(values),
        )

    summary: dict[str, int | None] = {
        "matching_ksa": base_scalar("COUNT(*)"),
        "linked_ksa": base_scalar("COUNT(kcl.link_id)"),
        "concepts": base_scalar("COUNT(DISTINCT oc.concept_id)"),
        "concepts_with_definition_text": base_scalar(
            """
            COUNT(DISTINCT CASE
              WHEN oc.definition IS NOT NULL AND TRIM(oc.definition) <> ''
              THEN oc.concept_id END)
            """
        ),
        "defined_concepts": base_scalar(
            """
            COUNT(DISTINCT CASE
              WHEN oc.definition IS NOT NULL
               AND TRIM(oc.definition) <> ''
               AND oc.definition_status = 'defined'
              THEN oc.concept_id END)
            """
        ),
        "missing_definition_concepts": base_scalar(
            """
            COUNT(DISTINCT CASE
              WHEN oc.concept_id IS NOT NULL
               AND (oc.definition IS NULL OR TRIM(oc.definition) = '')
              THEN oc.concept_id END)
            """
        ),
        "candidate_definition_concepts": base_scalar(
            """
            COUNT(DISTINCT CASE
              WHEN oc.definition_status = 'candidate'
              THEN oc.concept_id END)
            """
        ),
        "human_reviewed_concepts": base_scalar(
            """
            COUNT(DISTINCT CASE
              WHEN oc.review_status = 'human_reviewed'
              THEN oc.concept_id END)
            """
        ),
        "human_reviewed_definition_concepts": base_scalar(
            """
            COUNT(DISTINCT CASE
              WHEN oc.review_status = 'human_reviewed'
               AND oc.definition IS NOT NULL
               AND TRIM(oc.definition) <> ''
              THEN oc.concept_id END)
            """
        ),
        "model_preprocessed_concepts": base_scalar(
            """
            COUNT(DISTINCT CASE
              WHEN oc.review_status = 'model_preprocessed'
              THEN oc.concept_id END)
            """
        ),
        "label_candidate_concepts": _label_condition_concept_count(conn, where, values, "1 = 1"),
        "shortened_label_candidate_concepts": _label_condition_concept_count(
            conn,
            where,
            values,
            "label.source_method = 'rule_based_short_label_candidate'",
        ),
        "unchanged_label_candidate_concepts": _label_condition_concept_count(
            conn,
            where,
            values,
            "label.source_method = 'already_short_label'",
        ),
        "collision_label_candidate_concepts": _scoped_collision_label_concept_count(
            conn,
            where,
            values,
        ),
        "generic_label_candidate_concepts": _label_condition_concept_count(
            conn,
            where,
            values,
            KSA_DEFINITION_GENERIC_LABEL_SQL,
        ),
        "provenance_missing_label_candidate_concepts": base_scalar(
            """
            COUNT(DISTINCT CASE
              WHEN EXISTS (
                SELECT 1
                FROM ontology_concept_label_candidates orphan_label
                WHERE orphan_label.concept_id = oc.concept_id
                  AND orphan_label.source_ksa_id IS NULL
                  AND orphan_label.source_atomic_id IS NULL
              )
              THEN oc.concept_id END)
            """
        ),
        "human_reviewed_label_concepts": _label_condition_concept_count(
            conn,
            where,
            values,
            "label.review_status = 'human_reviewed'",
        ),
        "llm_reviewed_label_concepts": _label_condition_concept_count(
            conn,
            where,
            values,
            "label.review_status = 'llm_reviewed'",
        ),
        "llm_or_human_reviewed_label_candidate_concepts": _label_condition_concept_count(
            conn,
            where,
            values,
            "label.review_status IN ('human_reviewed', 'accepted', 'reviewed', 'llm_reviewed')",
        ),
        "human_confirmed_label_candidate_anomalies": _label_condition_concept_count(
            conn,
            where,
            values,
            "label.review_status IN ('human_reviewed', 'accepted', 'reviewed')",
        ),
        "concepts_with_meaning_candidates": base_scalar(
            """
            COUNT(DISTINCT CASE
              WHEN EXISTS (
                SELECT 1
                FROM ksa_meaning_candidates kmc
                WHERE kmc.concept_id = oc.concept_id
                  {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
              )
              THEN oc.concept_id END)
            """
        ),
        "llm_reviewed_meaning_concepts": base_scalar(
            """
            COUNT(DISTINCT CASE
              WHEN EXISTS (
                SELECT 1
                FROM ksa_meaning_candidates kmc
                WHERE kmc.concept_id = oc.concept_id
                  AND kmc.review_status = 'llm_reviewed'
                  {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
              )
              THEN oc.concept_id END)
            """
        ),
        "needs_review_meaning_concepts": base_scalar(
            """
            COUNT(DISTINCT CASE
              WHEN EXISTS (
                SELECT 1
                FROM ksa_meaning_candidates kmc
                WHERE kmc.concept_id = oc.concept_id
                  AND kmc.review_status = 'needs_review'
                  {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
              )
              THEN oc.concept_id END)
            """
        ),
        "candidate_meaning_concepts": base_scalar(
            """
            COUNT(DISTINCT CASE
              WHEN EXISTS (
                SELECT 1
                FROM ksa_meaning_candidates kmc
                WHERE kmc.concept_id = oc.concept_id
                  AND kmc.review_status = 'candidate'
                  {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
              )
              THEN oc.concept_id END)
            """
        ),
        "task_context_evidence_concepts": base_scalar(
            """
            COUNT(DISTINCT CASE
              WHEN EXISTS (
                SELECT 1
                FROM ksa_meaning_candidates kmc
                WHERE kmc.concept_id = oc.concept_id
                  AND kmc.source_method != 'term_definition_template'
                  {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
              )
              THEN oc.concept_id END)
            """
        ),
        "criteria_evidence_linked_ksa": base_scalar(
            """
            COUNT(DISTINCT CASE
              WHEN EXISTS (
                SELECT 1
                FROM criteria_concept_links ccl
                WHERE ccl.concept_id = oc.concept_id
              )
              THEN ki.ksa_id END)
            """
        ),
        "atomic_preprocessed_ksa": base_scalar(
            """
            COUNT(DISTINCT CASE
              WHEN EXISTS (
                SELECT 1
                FROM ksa_atomic_items atom
                WHERE atom.ksa_id = ki.ksa_id
              )
              THEN ki.ksa_id END)
            """
        ),
        "atomic_concept_linked_ksa": base_scalar(
            """
            COUNT(DISTINCT CASE
              WHEN EXISTS (
                SELECT 1
                FROM ksa_atomic_items atom
                JOIN ksa_atomic_concept_links acl ON acl.atomic_id = atom.atomic_id
                WHERE atom.ksa_id = ki.ksa_id
              )
              THEN ki.ksa_id END)
            """
        ),
    }
    summary["unlinked_ksa"] = max(int(summary["matching_ksa"] or 0) - int(summary["linked_ksa"] or 0), 0)
    summary["missing_label_candidate_concepts"] = max(
        int(summary["concepts"] or 0) - int(summary["label_candidate_concepts"] or 0),
        0,
    )
    return summary


def get_ksa_definitions(db_path: Path, params: dict[str, list[str]]) -> dict:
    try:
        conn = connect_db_for_read(
            db_path,
            required_tables=KSA_DEFINITION_DASHBOARD_TABLES,
        )
    except DashboardReadOnlyError as exc:
        return ksa_definition_dashboard_error_payload(exc)

    limit = safe_limit(params, default=100, maximum=500)
    try:
        if ksa_definition_label_state_requires_scope(params):
            return ksa_definition_dashboard_scope_required_payload(params, limit)
        where, values = ksa_definition_filter_sql(params)
        if ksa_definition_keyword_prefilter_enabled(params):
            matching_ksa_count = _populate_ksa_filter_table(conn, where, values)
            where = (
                _append_ksa_filter_where("")
                if matching_ksa_count
                else "WHERE 0 = 1"
            )
            values = []
        label_state = first(params, "label_state", "all")
        label_filter_active = False
        if ksa_definition_label_state_uses_concept_prefilter(params):
            _reset_label_filter_table(conn)
            if label_state == "collision":
                matching_label_count = _populate_scoped_collision_label_filter(conn, where, values)
            else:
                matching_label_count = _populate_scoped_label_condition_filter(
                    conn,
                    where,
                    values,
                    ksa_definition_label_state_condition(label_state),
                )
            where = (
                _append_label_filter_scope_where(where)
                if matching_label_count
                else (f"{where} AND 0 = 1" if where else "WHERE 0 = 1")
            )
            label_filter_active = bool(matching_label_count)
        label_review_condition = ksa_definition_label_review_status_condition(
            first(params, "label_review_status", "all")
        )
        display_label_conditions = [
            f"""
            label.label_id IN (
              SELECT active_label_filter.label_id
              FROM {KSA_DEFINITION_LABEL_FILTER_TABLE} active_label_filter
              WHERE active_label_filter.ksa_id = ki.ksa_id
            )
            """
            if label_filter_active
            else ksa_definition_label_state_condition(first(params, "label_state", "all"))
        ]
        if label_review_condition == "__missing__":
            display_label_conditions.append("0 = 1")
        elif label_review_condition != "1 = 1":
            display_label_conditions.append(label_review_condition)
        display_label_condition = " AND ".join(f"({condition})" for condition in display_label_conditions)
        scope_label_conditions = [
            f"""
            label.label_id IN (
              SELECT active_label_filter.label_id
              FROM {KSA_DEFINITION_LABEL_FILTER_TABLE} active_label_filter
              WHERE active_label_filter.ksa_id = ki.ksa_id
            )
            """
            if label_filter_active
            else ksa_definition_label_state_condition(first(params, "label_state", "all"))
        ]
        scope_label_condition = " AND ".join(f"({condition})" for condition in scope_label_conditions)
        compute_quality_summary = ksa_definition_quality_summary_enabled(params)
        summary_where = "WHERE 0"
        summary_values: list[object] = []
        summary_row = conn.execute(
            f"""
            SELECT
              COUNT(*) AS matching_ksa,
              COUNT(kcl.link_id) AS linked_ksa,
              COUNT(DISTINCT oc.concept_id) AS concepts,
              COUNT(DISTINCT CASE
                WHEN oc.definition IS NOT NULL AND TRIM(oc.definition) <> ''
                THEN oc.concept_id END
              ) AS concepts_with_definition_text,
              COUNT(DISTINCT CASE
                WHEN oc.definition IS NOT NULL AND TRIM(oc.definition) <> ''
                 AND oc.definition_status = 'defined'
                THEN oc.concept_id END
              ) AS defined_concepts,
              COUNT(DISTINCT CASE
                WHEN oc.concept_id IS NOT NULL
                 AND (oc.definition IS NULL OR TRIM(oc.definition) = '')
                THEN oc.concept_id END
              ) AS missing_definition_concepts,
              COUNT(DISTINCT CASE
                WHEN oc.definition_status = 'candidate'
                THEN oc.concept_id END
              ) AS candidate_definition_concepts,
              COUNT(DISTINCT CASE
                WHEN oc.review_status = 'human_reviewed'
                THEN oc.concept_id END
              ) AS human_reviewed_concepts,
              COUNT(DISTINCT CASE
                WHEN oc.review_status = 'human_reviewed'
                 AND oc.definition IS NOT NULL
                 AND TRIM(oc.definition) <> ''
                THEN oc.concept_id END
              ) AS human_reviewed_definition_concepts,
              COUNT(DISTINCT CASE
                WHEN oc.review_status = 'model_preprocessed'
                THEN oc.concept_id END
              ) AS model_preprocessed_concepts,
              COUNT(DISTINCT CASE
                WHEN {ksa_definition_label_exists_sql("1 = 1")}
                THEN oc.concept_id END
              ) AS label_candidate_concepts,
              COUNT(DISTINCT CASE
                WHEN {ksa_definition_label_exists_sql("label.source_method = 'rule_based_short_label_candidate'")}
                THEN oc.concept_id END
              ) AS shortened_label_candidate_concepts,
              COUNT(DISTINCT CASE
                WHEN {ksa_definition_label_exists_sql("label.source_method = 'already_short_label'")}
                THEN oc.concept_id END
              ) AS unchanged_label_candidate_concepts,
              COUNT(DISTINCT CASE
                WHEN {ksa_definition_label_exists_sql(KSA_DEFINITION_SCOPED_COLLISION_LABEL_SQL)}
                THEN oc.concept_id END
              ) AS collision_label_candidate_concepts,
              COUNT(DISTINCT CASE
                WHEN {ksa_definition_label_exists_sql(KSA_DEFINITION_GENERIC_LABEL_SQL)}
                THEN oc.concept_id END
              ) AS generic_label_candidate_concepts,
              COUNT(DISTINCT CASE
                WHEN EXISTS (
                  SELECT 1
                  FROM ontology_concept_label_candidates orphan_label
                  WHERE orphan_label.concept_id = oc.concept_id
                    AND orphan_label.source_ksa_id IS NULL
                    AND orphan_label.source_atomic_id IS NULL
                )
                THEN oc.concept_id END
              ) AS provenance_missing_label_candidate_concepts,
              COUNT(DISTINCT CASE
                WHEN {ksa_definition_label_exists_sql("label.review_status = 'human_reviewed'")}
                THEN oc.concept_id END
              ) AS human_reviewed_label_concepts,
              COUNT(DISTINCT CASE
                WHEN {ksa_definition_label_exists_sql("label.review_status = 'llm_reviewed'")}
                THEN oc.concept_id END
              ) AS llm_reviewed_label_concepts,
              COUNT(DISTINCT CASE
                WHEN {ksa_definition_label_exists_sql("label.review_status IN ('human_reviewed', 'accepted', 'reviewed', 'llm_reviewed')")}
                THEN oc.concept_id END
              ) AS llm_or_human_reviewed_label_candidate_concepts,
              COUNT(DISTINCT CASE
                WHEN {ksa_definition_label_exists_sql("label.review_status IN ('human_reviewed', 'accepted', 'reviewed')")}
                THEN oc.concept_id END
              ) AS human_confirmed_label_candidate_anomalies,
              COUNT(DISTINCT CASE
                WHEN EXISTS (
                  SELECT 1
                  FROM ksa_meaning_candidates kmc
                  WHERE kmc.concept_id = oc.concept_id
                )
                THEN oc.concept_id END
              ) AS concepts_with_meaning_candidates,
              COUNT(DISTINCT CASE
                WHEN EXISTS (
                  SELECT 1
                  FROM ksa_meaning_candidates kmc
                  WHERE kmc.concept_id = oc.concept_id
                    AND kmc.source_method != 'term_definition_template'
                )
                THEN oc.concept_id END
              ) AS task_context_evidence_concepts,
              COUNT(DISTINCT CASE
                WHEN EXISTS (
                  SELECT 1
                  FROM criteria_concept_links ccl
                  WHERE ccl.concept_id = oc.concept_id
                )
                THEN ki.ksa_id END
              ) AS criteria_evidence_linked_ksa,
              COUNT(DISTINCT CASE
                WHEN EXISTS (
                  SELECT 1
                  FROM ksa_atomic_items atom
                  WHERE atom.ksa_id = ki.ksa_id
                )
                THEN ki.ksa_id END
              ) AS atomic_preprocessed_ksa,
              COUNT(DISTINCT CASE
                WHEN EXISTS (
                  SELECT 1
                  FROM ksa_atomic_items atom
                  JOIN ksa_atomic_concept_links acl ON acl.atomic_id = atom.atomic_id
                  WHERE atom.ksa_id = ki.ksa_id
                )
                THEN ki.ksa_id END
              ) AS atomic_concept_linked_ksa
            {KSA_DEFINITION_BASE_FROM}
            {summary_where}
            """,
            summary_values,
        ).fetchone()
        summary = {key: int(summary_row[key] or 0) for key in summary_row.keys()}
        summary["unlinked_ksa"] = max(summary["matching_ksa"] - summary["linked_ksa"], 0)
        summary["missing_label_candidate_concepts"] = max(
            summary["concepts"] - summary["label_candidate_concepts"],
            0,
        )
        label_quality_flag_counts: dict[str, int] = {}
        if compute_quality_summary:
            summary = _precise_ksa_definition_summary(conn, where, values)
            summary["quality_review_label_candidate_concepts"] = _label_condition_concept_count(
                conn,
                where,
                values,
                KSA_DEFINITION_QUALITY_REVIEW_LABEL_SQL,
            )
            label_quality_flag_counts = _label_quality_flag_count_map(conn, where, values)
        else:
            summary = _broad_ksa_definition_summary(conn)

        rows = conn.execute(
            f"""
            SELECT
              ki.ksa_id,
              ki.ksa_type_name,
              ki.ksa_no,
              ki.ksa_text_raw,
              ki.ksa_text_refined,
              ki.review_status AS ksa_review_status,
              (
                SELECT GROUP_CONCAT(atom_text, ' | ')
                FROM (
                  SELECT atom.atom_text
                  FROM ksa_atomic_items atom
                  WHERE atom.ksa_id = ki.ksa_id
                  ORDER BY atom.atom_index
                  LIMIT 8
                )
              ) AS atomic_ksa_sample,
              (
                SELECT COUNT(*)
                FROM ksa_atomic_items atom
                WHERE atom.ksa_id = ki.ksa_id
              ) AS atomic_ksa_count,
              (
                SELECT GROUP_CONCAT(split_method, ' | ')
                FROM (
                  SELECT DISTINCT atom.split_method
                  FROM ksa_atomic_items atom
                  WHERE atom.ksa_id = ki.ksa_id
                  ORDER BY atom.split_method
                )
              ) AS atomic_split_methods,
              (
                SELECT COUNT(*)
                FROM ksa_atomic_items atom
                JOIN ksa_atomic_concept_links acl ON acl.atomic_id = atom.atomic_id
                WHERE atom.ksa_id = ki.ksa_id
              ) AS atomic_concept_link_count,
              c.major_code,
              c.major_name,
              c.middle_code,
              c.middle_name,
              c.small_code,
              c.small_name,
              c.sub_code,
              c.sub_name,
              cu.unit_code,
              cu.unit_name_raw,
              cu.unit_level_raw,
              ce.element_id,
              ce.element_no,
              ce.element_name_raw,
              kcl.link_status,
              oc.concept_id,
              oc.concept_name,
              oc.concept_type,
              oc.definition,
              oc.definition_source,
              COALESCE(oc.definition_status, 'unlinked') AS definition_status,
              COALESCE(oc.relation_status, 'unlinked') AS relation_status,
              COALESCE(oc.review_status, 'unlinked') AS concept_review_status,
              (
                SELECT label.label_id
                FROM ontology_concept_label_candidates label
                WHERE label.concept_id = oc.concept_id
                {KSA_DEFINITION_LABEL_SOURCE_SCOPE_SQL}
                {KSA_DEFINITION_LABEL_CURRENT_KSA_SQL}
                  AND ({display_label_condition})
                {KSA_DEFINITION_LABEL_DISPLAY_ORDER_SQL}
                LIMIT 1
              ) AS short_label_id,
              (
                SELECT label.label_text
                FROM ontology_concept_label_candidates label
                WHERE label.concept_id = oc.concept_id
                {KSA_DEFINITION_LABEL_SOURCE_SCOPE_SQL}
                {KSA_DEFINITION_LABEL_CURRENT_KSA_SQL}
                  AND ({display_label_condition})
                {KSA_DEFINITION_LABEL_DISPLAY_ORDER_SQL}
                LIMIT 1
              ) AS short_label_candidate,
              (
                SELECT label.source_text
                FROM ontology_concept_label_candidates label
                WHERE label.concept_id = oc.concept_id
                {KSA_DEFINITION_LABEL_SOURCE_SCOPE_SQL}
                {KSA_DEFINITION_LABEL_CURRENT_KSA_SQL}
                  AND ({display_label_condition})
                {KSA_DEFINITION_LABEL_DISPLAY_ORDER_SQL}
                LIMIT 1
              ) AS short_label_source_text,
              (
                SELECT label.source_ksa_id
                FROM ontology_concept_label_candidates label
                WHERE label.concept_id = oc.concept_id
                {KSA_DEFINITION_LABEL_SOURCE_SCOPE_SQL}
                {KSA_DEFINITION_LABEL_CURRENT_KSA_SQL}
                  AND ({display_label_condition})
                {KSA_DEFINITION_LABEL_DISPLAY_ORDER_SQL}
                LIMIT 1
              ) AS short_label_source_ksa_id,
              (
                SELECT label.source_atomic_id
                FROM ontology_concept_label_candidates label
                WHERE label.concept_id = oc.concept_id
                {KSA_DEFINITION_LABEL_SOURCE_SCOPE_SQL}
                {KSA_DEFINITION_LABEL_CURRENT_KSA_SQL}
                  AND ({display_label_condition})
                {KSA_DEFINITION_LABEL_DISPLAY_ORDER_SQL}
                LIMIT 1
              ) AS short_label_source_atomic_id,
              (
                SELECT CASE
                  WHEN label.source_ksa_id = ki.ksa_id THEN 'source_ksa_id'
                  WHEN EXISTS (
                    SELECT 1
                    FROM ksa_atomic_items current_label_atom
                    WHERE current_label_atom.ksa_id = ki.ksa_id
                      AND current_label_atom.atomic_id = label.source_atomic_id
                  ) THEN 'source_atomic_id'
                  ELSE 'not_current_row'
                END
                FROM ontology_concept_label_candidates label
                WHERE label.concept_id = oc.concept_id
                {KSA_DEFINITION_LABEL_SOURCE_SCOPE_SQL}
                {KSA_DEFINITION_LABEL_CURRENT_KSA_SQL}
                  AND ({display_label_condition})
                {KSA_DEFINITION_LABEL_DISPLAY_ORDER_SQL}
                LIMIT 1
              ) AS short_label_provenance_match,
              (
                SELECT label.source_scope_key
                FROM ontology_concept_label_candidates label
                WHERE label.concept_id = oc.concept_id
                {KSA_DEFINITION_LABEL_SOURCE_SCOPE_SQL}
                {KSA_DEFINITION_LABEL_CURRENT_KSA_SQL}
                  AND ({display_label_condition})
                {KSA_DEFINITION_LABEL_DISPLAY_ORDER_SQL}
                LIMIT 1
              ) AS short_label_source_scope_key,
              (
                SELECT label.source_method
                FROM ontology_concept_label_candidates label
                WHERE label.concept_id = oc.concept_id
                {KSA_DEFINITION_LABEL_SOURCE_SCOPE_SQL}
                {KSA_DEFINITION_LABEL_CURRENT_KSA_SQL}
                  AND ({display_label_condition})
                {KSA_DEFINITION_LABEL_DISPLAY_ORDER_SQL}
                LIMIT 1
              ) AS short_label_source_method,
              (
                SELECT label.confidence_score
                FROM ontology_concept_label_candidates label
                WHERE label.concept_id = oc.concept_id
                {KSA_DEFINITION_LABEL_SOURCE_SCOPE_SQL}
                {KSA_DEFINITION_LABEL_CURRENT_KSA_SQL}
                  AND ({display_label_condition})
                {KSA_DEFINITION_LABEL_DISPLAY_ORDER_SQL}
                LIMIT 1
              ) AS short_label_confidence,
              (
                SELECT label.review_status
                FROM ontology_concept_label_candidates label
                WHERE label.concept_id = oc.concept_id
                {KSA_DEFINITION_LABEL_SOURCE_SCOPE_SQL}
                {KSA_DEFINITION_LABEL_CURRENT_KSA_SQL}
                  AND ({display_label_condition})
                {KSA_DEFINITION_LABEL_DISPLAY_ORDER_SQL}
                LIMIT 1
              ) AS short_label_review_status,
              (
                SELECT COUNT(*)
                FROM ontology_concept_label_candidates label
                WHERE label.concept_id = oc.concept_id
                {KSA_DEFINITION_LABEL_SOURCE_SCOPE_SQL}
                {KSA_DEFINITION_LABEL_CURRENT_KSA_SQL}
                  AND ({display_label_condition})
              ) AS short_label_candidate_count,
              CASE
                WHEN oc.concept_id IS NULL THEN '01_unlinked'
                WHEN oc.review_status = 'human_reviewed'
                 AND oc.definition IS NOT NULL
                 AND TRIM(oc.definition) <> ''
                THEN '04_human_reviewed_definition'
                WHEN COALESCE(oc.definition_source, '') = 'ksa_meaning_candidates.term_definition_template'
                THEN '03_model_term_definition_candidate'
                WHEN COALESCE(oc.definition_source, '') = 'ksa_meaning_candidates.task_context_template'
                THEN '03_legacy_task_context_definition'
                WHEN oc.definition_status = 'candidate'
                 OR oc.review_status = 'model_preprocessed'
                 OR COALESCE(oc.definition_source, '') LIKE 'ksa_meaning_candidates.%'
                THEN '03_model_candidate_definition'
                WHEN oc.definition IS NOT NULL AND TRIM(oc.definition) <> ''
                THEN '03_definition_text_present'
                ELSE '02_concept_linked_no_definition'
              END AS preprocessing_stage,
              CASE
                WHEN oc.concept_id IS NULL THEN 'unlinked'
                WHEN oc.review_status = 'human_reviewed'
                 AND oc.definition IS NOT NULL
                 AND TRIM(oc.definition) <> ''
                THEN 'human_reviewed_definition'
                WHEN COALESCE(oc.definition_source, '') = 'ksa_meaning_candidates.term_definition_template'
                THEN 'model_term_definition_candidate'
                WHEN COALESCE(oc.definition_source, '') = 'ksa_meaning_candidates.task_context_template'
                THEN 'legacy_task_context_in_definition'
                WHEN oc.definition_status = 'candidate'
                 OR oc.review_status = 'model_preprocessed'
                 OR COALESCE(oc.definition_source, '') LIKE 'ksa_meaning_candidates.%'
                THEN 'model_candidate_definition'
                WHEN oc.definition IS NOT NULL AND TRIM(oc.definition) <> ''
                THEN 'definition_text_unclassified'
                ELSE 'missing_definition'
              END AS definition_kind,
              (
                SELECT GROUP_CONCAT(alias_text, ' | ')
                FROM (
                  SELECT alias_text
                  FROM ontology_concept_aliases alias
                  WHERE alias.concept_id = oc.concept_id
                  ORDER BY alias_text
                  LIMIT 5
                )
              ) AS alias_sample,
              (
                SELECT COUNT(DISTINCT ccl.criteria_id)
                FROM criteria_concept_links ccl
                WHERE ccl.concept_id = oc.concept_id
              ) AS related_criteria_count,
              (
                SELECT kmc.meaning_id
                FROM ksa_meaning_candidates kmc
                WHERE kmc.concept_id = oc.concept_id
                  AND kmc.source_method = 'term_definition_template'
                  {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
                ORDER BY kmc.confidence_score DESC, kmc.meaning_id
                LIMIT 1
              ) AS term_definition_meaning_id,
              (
                SELECT kmc.meaning_text
                FROM ksa_meaning_candidates kmc
                WHERE kmc.concept_id = oc.concept_id
                  AND kmc.source_method = 'term_definition_template'
                  {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
                ORDER BY kmc.confidence_score DESC, kmc.meaning_id
                LIMIT 1
              ) AS term_definition_candidate,
              (
                SELECT kmc.evidence_text
                FROM ksa_meaning_candidates kmc
                WHERE kmc.concept_id = oc.concept_id
                  AND kmc.source_method = 'term_definition_template'
                  {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
                ORDER BY kmc.confidence_score DESC, kmc.meaning_id
                LIMIT 1
              ) AS term_definition_evidence,
              (
                SELECT kmc.meaning_role
                FROM ksa_meaning_candidates kmc
                WHERE kmc.concept_id = oc.concept_id
                  AND kmc.source_method = 'term_definition_template'
                  {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
                ORDER BY kmc.confidence_score DESC, kmc.meaning_id
                LIMIT 1
              ) AS term_definition_role,
              (
                SELECT kmc.review_status
                FROM ksa_meaning_candidates kmc
                WHERE kmc.concept_id = oc.concept_id
                  AND kmc.source_method = 'term_definition_template'
                  {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
                ORDER BY kmc.confidence_score DESC, kmc.meaning_id
                LIMIT 1
              ) AS term_definition_review_status,
              (
                SELECT kmc.confidence_score
                FROM ksa_meaning_candidates kmc
                WHERE kmc.concept_id = oc.concept_id
                  AND kmc.source_method = 'term_definition_template'
                  {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
                ORDER BY kmc.confidence_score DESC, kmc.meaning_id
                LIMIT 1
              ) AS term_definition_confidence,
              (
                SELECT kmc.meaning_text
                FROM ksa_meaning_candidates kmc
                WHERE kmc.concept_id = oc.concept_id
                  AND kmc.source_method != 'term_definition_template'
                  {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
                ORDER BY
                  CASE WHEN kmc.source_method = 'task_context_template' THEN 0 ELSE 1 END,
                  kmc.confidence_score DESC,
                  kmc.meaning_id
                LIMIT 1
              ) AS meaning_candidate,
              (
                SELECT kmc.meaning_role
                FROM ksa_meaning_candidates kmc
                WHERE kmc.concept_id = oc.concept_id
                  AND kmc.source_method != 'term_definition_template'
                  {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
                ORDER BY
                  CASE WHEN kmc.source_method = 'task_context_template' THEN 0 ELSE 1 END,
                  kmc.confidence_score DESC,
                  kmc.meaning_id
                LIMIT 1
              ) AS meaning_role,
              (
                SELECT kmc.review_status
                FROM ksa_meaning_candidates kmc
                WHERE kmc.concept_id = oc.concept_id
                  AND kmc.source_method != 'term_definition_template'
                  {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
                ORDER BY
                  CASE WHEN kmc.source_method = 'task_context_template' THEN 0 ELSE 1 END,
                  kmc.confidence_score DESC,
                  kmc.meaning_id
                LIMIT 1
              ) AS meaning_review_status,
              (
                SELECT kmc.source_method
                FROM ksa_meaning_candidates kmc
                WHERE kmc.concept_id = oc.concept_id
                  AND kmc.source_method != 'term_definition_template'
                  {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
                ORDER BY
                  CASE WHEN kmc.source_method = 'task_context_template' THEN 0 ELSE 1 END,
                  kmc.confidence_score DESC,
                  kmc.meaning_id
                LIMIT 1
              ) AS meaning_source_method
            {KSA_DEFINITION_BASE_FROM}
            {where}
            ORDER BY
              CASE
                WHEN oc.concept_id IS NULL THEN 0
                WHEN oc.definition IS NULL OR TRIM(oc.definition) = '' THEN 1
                ELSE 2
              END,
              c.major_code,
              c.middle_code,
              c.small_code,
              c.sub_code,
              cu.unit_code,
              CAST(ce.element_no AS INTEGER),
              ki.ksa_type_code,
              CAST(ki.ksa_no AS INTEGER),
              ki.ksa_id
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()

        items = [dict(row) for row in rows]
        _attach_latest_ksa_label_review_audits(conn, items)
        _attach_latest_ksa_meaning_review_audits(conn, items)
        for item in items:
            _attach_ksa_short_label_transform_metrics(item)
            item["short_label_quality_flags"] = (
                ksa_label_quality_flags(
                    item.get("short_label_source_text") or "",
                    item.get("short_label_candidate") or "",
                    item.get("concept_type") or "",
                )
                if item.get("short_label_candidate")
                else []
            )
            item["short_label_quality_flag_count"] = len(item["short_label_quality_flags"])
            priority, reason = _ksa_short_label_review_priority(item)
            item["short_label_review_priority"] = priority
            item["short_label_review_reason"] = reason
            item["short_label_is_machine_screened"] = (
                item.get("short_label_review_status") == "llm_reviewed"
            )
            item["short_label_is_human_approved"] = item.get("short_label_review_status") in {
                "human_reviewed",
                "accepted",
                "reviewed",
            }
            definition_source = str(item.get("definition_source") or "")
            concept_review_status = str(item.get("concept_review_status") or "")
            item["definition_is_machine_draft"] = (
                item.get("definition_status") == "candidate"
                or concept_review_status in {"model_preprocessed", "llm_reviewed"}
                or definition_source.startswith("ksa_meaning_candidates.")
                or definition_source == "ksa_meaning_candidate_promotion"
            )
            item["definition_is_human_approved"] = item.get("concept_review_status") in {
                "human_reviewed",
                "accepted",
                "reviewed",
            }
            item["term_definition_candidate_is_machine_draft"] = (
                item.get("term_definition_review_status") in {"candidate", "llm_reviewed"}
            )
            item.update(_dashboard_task_evidence(conn, item.get("concept_id")))
            item["atomic_label_candidates"] = _dashboard_atomic_label_candidates(
                conn,
                item.get("ksa_id"),
            )
            item["atomic_label_candidate_count"] = len(item["atomic_label_candidates"])

        if compute_quality_summary:
            count_maps = {
                "definition_status_counts": _grouped_count_map(
                    conn,
                    "COALESCE(oc.definition_status, 'unlinked')",
                    where,
                    values,
                ),
                "concept_type_counts": _grouped_count_map(
                    conn,
                    "COALESCE(oc.concept_type, 'unlinked')",
                    where,
                    values,
                ),
                "concept_review_status_counts": _grouped_count_map(
                    conn,
                    "COALESCE(oc.review_status, 'unlinked')",
                    where,
                    values,
                ),
                "label_review_status_counts": _grouped_count_map(
                    conn,
                    f"""
                    COALESCE((
                      SELECT label.review_status
                      FROM ontology_concept_label_candidates label
                      WHERE label.concept_id = oc.concept_id
                      {KSA_DEFINITION_LABEL_SOURCE_SCOPE_SQL}
                      {KSA_DEFINITION_LABEL_CURRENT_KSA_SQL}
                        AND ({display_label_condition})
                      {KSA_DEFINITION_LABEL_DISPLAY_ORDER_SQL}
                      LIMIT 1
                    ), 'missing')
                    """,
                    where,
                    values,
                ),
                "meaning_review_status_counts": _grouped_count_map(
                    conn,
                    """
                    COALESCE((
                      SELECT kmc.review_status
                      FROM ksa_meaning_candidates kmc
                      WHERE kmc.concept_id = oc.concept_id
                      {KSA_DEFINITION_MEANING_CURRENT_KSA_SQL}
                      ORDER BY
                        CASE kmc.meaning_role
                          WHEN 'term_definition_candidate' THEN 0
                          ELSE 1
                        END,
                        kmc.confidence_score DESC,
                        kmc.meaning_id
                      LIMIT 1
                    ), 'missing')
                    """,
                    where,
                    values,
                ),
            }
            scope_params = {key: list(value) for key, value in params.items()}
            scope_params["label_review_status"] = ["all"]
            scope_where, scope_values = ksa_definition_filter_sql(scope_params)
            label_review_scope_status_counts = _grouped_count_map(
                conn,
                f"""
                COALESCE((
                  SELECT label.review_status
                  FROM ontology_concept_label_candidates label
                  WHERE label.concept_id = oc.concept_id
                  {KSA_DEFINITION_LABEL_SOURCE_SCOPE_SQL}
                  {KSA_DEFINITION_LABEL_CURRENT_KSA_SQL}
                    AND ({scope_label_condition})
                  {KSA_DEFINITION_LABEL_DISPLAY_ORDER_SQL}
                  LIMIT 1
                ), 'missing')
                """,
                scope_where,
                scope_values,
            )
            label_review_scope_human_action_counts = _label_review_human_action_count_map(
                conn,
                scope_where,
                scope_values,
                scope_label_condition,
            )
        else:
            count_maps = _broad_ksa_definition_count_maps(conn, summary)
            label_review_scope_status_counts = count_maps["label_review_status_counts"]
            label_review_scope_human_action_counts = {}

        return {
            "ok": True,
            "schema": "ncs_ksa_definition_dashboard_v1",
            "generated_at": now_utc(),
            "limit": limit,
            "filters": {
                "major_code": first(params, "major_code"),
                "middle_code": first(params, "middle_code"),
                "small_code": first(params, "small_code"),
                "sub_code": first(params, "sub_code"),
                "keyword": first(params, "keyword"),
                "concept_type": first(params, "concept_type") or "all",
                "definition_state": first(params, "definition_state", "all"),
                "label_state": first(params, "label_state", "all"),
                "label_review_status": first(params, "label_review_status", "all"),
                "meaning_review_status": first(params, "meaning_review_status", "all"),
            },
            "policy": {
                "raw_ksa_preserved": True,
                "current_definition_field": "ontology_concepts.definition",
                "term_definition_candidate_field": "ksa_meaning_candidates.meaning_text",
                "term_definition_candidate_filter": "ksa_meaning_candidates.source_method='term_definition_template'",
                "task_context_candidate_field": "ksa_meaning_candidates.meaning_text",
                "status_update_allowed": False,
                "note": (
                    "KSA raw text is source evidence. The stored ontology definition and "
                    "term/task-context meaning candidates are displayed separately without "
                    "approving review status."
                ),
            },
            "summary": summary,
            "definition_status_counts": count_maps["definition_status_counts"],
            "concept_type_counts": count_maps["concept_type_counts"],
            "concept_review_status_counts": count_maps["concept_review_status_counts"],
            "label_review_status_counts": count_maps["label_review_status_counts"],
            "label_review_scope_status_counts": label_review_scope_status_counts,
            "label_review_scope_human_action_counts": label_review_scope_human_action_counts,
            "meaning_review_status_counts": count_maps["meaning_review_status_counts"],
            "label_review_progress": _label_review_progress_from_counts(
                count_maps["label_review_status_counts"],
                unit=(
                    "filtered_ksa_rows"
                    if compute_quality_summary
                    else "representative_concepts_broad_summary"
                ),
                unit_label=(
                    "Filtered KSA rows"
                    if compute_quality_summary
                    else "Broad representative concepts"
                ),
            ),
            "label_review_scope_progress": _label_review_progress_from_counts(
                label_review_scope_status_counts,
                unit=(
                    "scope_ksa_rows_excluding_label_review_filter"
                    if compute_quality_summary
                    else "representative_concepts_broad_summary"
                ),
                unit_label=(
                    "Scope KSA rows excluding label review filter"
                    if compute_quality_summary
                    else "Broad representative concepts"
                ),
            ),
            "meaning_review_progress": _label_review_progress_from_counts(
                count_maps["meaning_review_status_counts"],
                unit=(
                    "filtered_term_definition_candidates"
                    if compute_quality_summary
                    else "representative_term_definition_candidates_broad_summary"
                ),
                unit_label=(
                    "Filtered term definition candidates"
                    if compute_quality_summary
                    else "Broad term definition candidates"
                ),
            ),
            "label_quality_flag_counts": label_quality_flag_counts,
            "items": items,
        }
    finally:
        conn.close()


def _label_review_progress_from_counts(
    counts: dict[str, int],
    *,
    unit: str = "filtered_ksa_rows",
    unit_label: str = "Filtered KSA rows",
) -> dict[str, int | float | str]:
    pending = int(counts.get("candidate", 0) or 0)
    human_reviewed = sum(
        int(counts.get(status, 0) or 0)
        for status in ("human_reviewed", "accepted", "reviewed")
    )
    llm_reviewed = int(counts.get("llm_reviewed", 0) or 0)
    needs_review = int(counts.get("needs_review", 0) or 0)
    rejected = int(counts.get("rejected", 0) or 0)
    missing = int(counts.get("missing", 0) or 0)
    total = sum(int(value or 0) for value in counts.values())
    automated_actioned = llm_reviewed + needs_review
    actioned = human_reviewed + needs_review + rejected
    checked = human_reviewed
    machine_screened = llm_reviewed
    return {
        "total": total,
        "pending": pending,
        "human_reviewed": human_reviewed,
        "llm_reviewed": llm_reviewed,
        "checked": checked,
        "human_checked": human_reviewed,
        "machine_screened": machine_screened,
        "needs_review": needs_review,
        "rejected": rejected,
        "missing": missing,
        "automated_actioned": automated_actioned,
        "actioned": actioned,
        "coverage_percent": round((actioned * 100 / total), 1) if total else 0.0,
        "actioned_percent": round((actioned * 100 / total), 1) if total else 0.0,
        "automated_actioned_percent": round((automated_actioned * 100 / total), 1)
        if total
        else 0.0,
        "checked_percent": round((checked * 100 / total), 1) if total else 0.0,
        "human_checked_percent": round((human_reviewed * 100 / total), 1)
        if total
        else 0.0,
        "machine_screened_percent": round((machine_screened * 100 / total), 1)
        if total
        else 0.0,
        "human_reviewed_percent": round((human_reviewed * 100 / total), 1)
        if total
        else 0.0,
        "unit": unit,
        "unit_label": unit_label,
    }


def _ksa_short_label_review_priority(item: dict) -> tuple[str, str]:
    status = str(item.get("short_label_review_status") or "")
    if not item.get("short_label_candidate"):
        return "missing", "no_short_label_candidate"
    if status in {"human_reviewed", "accepted", "reviewed"}:
        return "completed", "already_human_checked"
    if status == "llm_reviewed":
        return "machine_reviewed", "automated_llm_reviewed_not_approval"
    if status == "rejected":
        return "completed", "label_rejected"
    if status == "needs_review":
        if item.get("short_label_last_review_action") == "ksa_label_needs_revision":
            return "high", "human_marked_needs_review"
        return "high", "auto_quality_needs_review"
    flags = set(item.get("short_label_quality_flags") or [])
    high_flags = {
        "unbalanced_parentheses",
        "dangling_enum_suffix",
        "skill_suffix_stripped_to_generic",
        "very_low_label_source_ratio",
        "symbol_heavy",
    }
    medium_flags = {
        "changed_near_full_length",
        "generic_or_low_specificity",
        "digit_heavy",
        "short_acronym_needs_context",
    }
    for flag in sorted(high_flags):
        if flag in flags:
            return "high", flag
    for flag in sorted(medium_flags):
        if flag in flags:
            return "medium", flag
    if status == "candidate":
        return "low", "pending_candidate"
    return "low", status or "unknown_status"


def _attach_ksa_short_label_transform_metrics(item: dict) -> None:
    source = str(item.get("short_label_source_text") or "").strip()
    label = str(item.get("short_label_candidate") or "").strip()
    source_len = len(source)
    label_len = len(label)
    item["short_label_source_length"] = source_len if source else None
    item["short_label_label_length"] = label_len if label else None
    item["short_label_removed_char_count"] = (
        max(source_len - label_len, 0) if source and label else None
    )
    item["short_label_length_ratio"] = (
        round(label_len / source_len, 3) if source_len and label else None
    )
    if not label:
        state = "missing"
    elif not source:
        state = "source_missing"
    elif source == label:
        state = "unchanged"
    elif label_len < source_len:
        state = "shortened"
    else:
        state = "expanded_or_rewritten"
    item["short_label_transform_state"] = state


def _attach_latest_ksa_label_review_audits(conn, items: list[dict]) -> None:
    default_audit_fields = {
        "short_label_last_review_action": None,
        "short_label_last_review_previous_status": None,
        "short_label_last_review_status": None,
        "short_label_last_reviewer_id": None,
        "short_label_last_review_note": None,
        "short_label_last_review_rationale": None,
        "short_label_last_reviewed_at": None,
        "short_label_last_review_packet": None,
        "short_label_last_review_tool": None,
    }
    for item in items:
        item.update(default_audit_fields)
    label_ids = sorted(
        {
            str(item["short_label_id"])
            for item in items
            if item.get("short_label_id") is not None
        }
    )
    if not label_ids or not table_exists(conn, "review_audit_log"):
        return
    placeholders = ",".join("?" for _ in label_ids)
    audit_rows = conn.execute(
        f"""
        SELECT
          entity_id,
          action,
          previous_status,
          new_status,
          reviewer_id,
          notes,
          rationale,
          source_decision_packet,
          created_by_tool,
          created_at
        FROM review_audit_log
        WHERE entity_type = 'ontology_concept_label_candidate'
          AND entity_id IN ({placeholders})
        ORDER BY entity_id, created_at DESC, rowid DESC
        """,
        label_ids,
    ).fetchall()
    latest_by_label_id: dict[str, sqlite3.Row] = {}
    for row in audit_rows:
        latest_by_label_id.setdefault(str(row["entity_id"]), row)
    for item in items:
        row = latest_by_label_id.get(str(item.get("short_label_id")))
        if row is None:
            continue
        item.update(
            {
                "short_label_last_review_action": row["action"],
                "short_label_last_review_previous_status": row["previous_status"],
                "short_label_last_review_status": row["new_status"],
                "short_label_last_reviewer_id": row["reviewer_id"],
                "short_label_last_review_note": row["notes"],
                "short_label_last_review_rationale": row["rationale"],
                "short_label_last_reviewed_at": row["created_at"],
                "short_label_last_review_packet": row["source_decision_packet"],
                "short_label_last_review_tool": row["created_by_tool"],
            }
        )


def _attach_latest_ksa_meaning_review_audits(conn, items: list[dict]) -> None:
    default_audit_fields = {
        "term_definition_last_review_action": None,
        "term_definition_last_review_previous_status": None,
        "term_definition_last_review_status": None,
        "term_definition_last_reviewer_id": None,
        "term_definition_last_review_note": None,
        "term_definition_last_review_rationale": None,
        "term_definition_last_reviewed_at": None,
        "term_definition_last_review_packet": None,
        "term_definition_last_review_tool": None,
    }
    for item in items:
        item.update(default_audit_fields)
    meaning_ids = sorted(
        {
            str(item["term_definition_meaning_id"])
            for item in items
            if item.get("term_definition_meaning_id") is not None
        }
    )
    if not meaning_ids or not table_exists(conn, "review_audit_log"):
        return
    placeholders = ",".join("?" for _ in meaning_ids)
    audit_rows = conn.execute(
        f"""
        SELECT
          entity_id,
          action,
          previous_status,
          new_status,
          reviewer_id,
          notes,
          rationale,
          source_decision_packet,
          created_by_tool,
          created_at
        FROM review_audit_log
        WHERE entity_type = 'ksa_meaning_candidate'
          AND entity_id IN ({placeholders})
        ORDER BY entity_id, created_at DESC, rowid DESC
        """,
        meaning_ids,
    ).fetchall()
    latest_by_meaning_id: dict[str, sqlite3.Row] = {}
    for row in audit_rows:
        latest_by_meaning_id.setdefault(str(row["entity_id"]), row)
    for item in items:
        row = latest_by_meaning_id.get(str(item.get("term_definition_meaning_id")))
        if row is None:
            continue
        item.update(
            {
                "term_definition_last_review_action": row["action"],
                "term_definition_last_review_previous_status": row["previous_status"],
                "term_definition_last_review_status": row["new_status"],
                "term_definition_last_reviewer_id": row["reviewer_id"],
                "term_definition_last_review_note": row["notes"],
                "term_definition_last_review_rationale": row["rationale"],
                "term_definition_last_reviewed_at": row["created_at"],
                "term_definition_last_review_packet": row["source_decision_packet"],
                "term_definition_last_review_tool": row["created_by_tool"],
            }
        )


def render_ksa_definition_dashboard_html() -> str:
    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KSA Definition Dashboard</title>
  <style>
    :root { --ink:#172033; --muted:#667085; --line:#d9e0ea; --bg:#f6f8fb; --panel:#fff; --accent:#0f766e; --bad:#b91c1c; --warn:#b45309; --ok:#15803d; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Segoe UI, Arial, "Malgun Gothic", sans-serif; background:var(--bg); color:var(--ink); line-height:1.45; }
    main { max-width:1480px; margin:0 auto; padding:24px; }
    h1 { margin:0 0 6px; font-size:26px; }
    h2 { margin:18px 0 10px; font-size:18px; }
    .muted { color:var(--muted); }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; margin:14px 0; }
    .toolbar { display:flex; flex-wrap:wrap; gap:8px; align-items:end; }
    .review-lead { color:var(--muted); margin:0 0 10px; }
    .review-lead + p.muted { display:none; }
    .review-guide { display:grid; gap:8px; }
    .review-guide ol { margin:6px 0 0 20px; padding:0; }
    .answer-box { display:none; }
    .scope-header { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(260px,.65fr); gap:14px; align-items:start; }
    .scope-progress { border:1px solid var(--line); border-radius:8px; background:#f8fafc; padding:12px; display:grid; gap:8px; }
    .scope-progress strong { font-size:20px; }
    .progress { height:9px; border-radius:999px; background:#e2e8f0; overflow:hidden; }
    .progress span { display:block; height:100%; background:var(--accent); border-radius:999px; }
    .major-grid { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:8px; margin-top:10px; }
    .major-tile, .node { width:100%; min-height:48px; border:1px solid var(--line); border-radius:8px; background:#fff; color:var(--ink); text-align:left; padding:9px 10px; display:grid; gap:4px; cursor:pointer; }
    .major-tile.active, .node.active { border-color:var(--accent); background:#ecfdf5; box-shadow:0 0 0 2px rgba(15,118,110,.12); }
    .tile-title, .node-title { font-weight:700; }
    .tile-meta, .node-sub { color:var(--muted); font-size:12px; }
    .taxonomy-columns { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-top:12px; }
    .taxonomy-column { border:1px solid var(--line); border-radius:8px; background:#fbfdff; padding:10px; min-height:180px; }
    .taxonomy-column header { display:flex; justify-content:space-between; gap:8px; align-items:center; margin-bottom:8px; }
    .taxonomy-list { display:grid; gap:6px; max-height:260px; overflow:auto; padding-right:2px; }
    .field { display:grid; gap:4px; min-width:120px; }
    label { font-size:12px; font-weight:700; color:#445166; }
    input, select { min-height:38px; border:1px solid #cbd5e1; border-radius:6px; padding:8px 10px; font:inherit; background:#fff; }
    input.code { width:72px; }
    input.keyword { min-width:260px; }
    button, a.button-link { min-height:38px; border:1px solid var(--accent); border-radius:6px; background:var(--accent); color:#fff; padding:8px 12px; font-weight:700; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; }
    button.secondary, a.button-link.secondary { background:#fff; color:var(--accent); }
    .summary { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:10px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:10px; background:#fbfdff; min-height:78px; }
    .metric span { display:block; color:var(--muted); font-size:12px; }
    .metric strong { font-size:22px; }
    .metric.good strong { color:var(--ok); }
    .metric.warn strong { color:var(--warn); }
    .metric.bad strong { color:var(--bad); }
    .pipeline { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; margin-top:12px; }
    .stage { border:1px solid var(--line); border-radius:8px; padding:12px; background:#fff; }
    .stage b { display:block; font-size:14px; margin-bottom:4px; }
    .stage strong { display:block; font-size:22px; margin-top:6px; }
    .status { min-height:24px; margin-top:10px; font-weight:700; }
    .status.error { color:var(--bad); }
    .scroll { overflow:auto; max-height:68vh; border:1px solid var(--line); border-radius:8px; }
    .table-note { margin:0 0 10px; color:var(--muted); }
    table { width:100%; border-collapse:collapse; background:#fff; table-layout:fixed; }
    th, td { border-bottom:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; font-size:13px; overflow-wrap:anywhere; }
    th { position:sticky; top:0; background:#eef2f5; z-index:1; }
    .pill { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:2px 7px; margin:0 4px 4px 0; background:#fff; font-size:12px; }
    .pill.warn { border-color:#f59e0b; color:#92400e; background:#fffbeb; }
    .pill.good { border-color:#22c55e; color:#166534; background:#f0fdf4; }
    .pill.bad { border-color:#ef4444; color:#991b1b; background:#fef2f2; }
    .definition { white-space:pre-wrap; }
    .preprocess-block { display:grid; gap:8px; }
    .preprocess-block b { display:block; margin-bottom:2px; }
    .source-block { display:grid; gap:6px; }
    .raw-card { border:1px solid #cbd5e1; background:#f8fafc; border-radius:8px; padding:10px; display:grid; gap:8px; }
    .raw-card strong { display:block; font-size:14px; }
    .preprocess-summary { border:1px solid #99f6e4; background:#f0fdfa; border-radius:8px; padding:10px; }
    .preprocess-summary b { display:block; color:#0f766e; margin-bottom:4px; }
    .label-card { border:1px solid #f59e0b; background:#fffbeb; border-radius:8px; padding:10px; }
    .label-card strong { display:block; font-size:15px; color:#92400e; }
    .review-actions { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
    .review-actions button { min-height:30px; padding:4px 8px; font-size:12px; }
    .review-actions button.good { border-color:#15803d; background:#15803d; }
    .review-actions button.bad { border-color:#b91c1c; background:#b91c1c; }
    .review-audit { border-top:1px solid #f59e0b; margin-top:8px; padding-top:8px; display:grid; gap:4px; }
    .review-details { margin-top:8px; border:1px solid var(--line); border-radius:8px; background:#fff; padding:8px 10px; }
    .review-details summary { cursor:pointer; font-weight:700; color:#334155; }
    input.reviewer { min-width:150px; }
    input.review-note { min-width:320px; }
    .chain { display:grid; gap:10px; }
    .chain-step { border-left:3px solid #cbd5e1; padding-left:10px; }
    .chain-step.good { border-left-color:var(--ok); }
    .chain-step.warn { border-left-color:var(--warn); }
    .empty-definition { color:var(--warn); font-weight:700; }
    .notice { border-left:4px solid var(--accent); background:#eefaf7; padding:10px 12px; margin-top:10px; }
    .answer-box { display:none; border:1px solid #f59e0b; background:#fffbeb; border-radius:8px; padding:12px; margin:12px 0 0; }
    .answer-box strong { display:block; color:#92400e; margin-bottom:4px; }
    .evidence-map { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin-top:10px; }
    .evidence-map div { border:1px solid var(--border); border-radius:8px; background:#fff; padding:8px; }
    .evidence-map b { display:block; color:#111827; margin-bottom:3px; }
    .evidence-preview { display:grid; gap:6px; margin-top:6px; }
    .evidence-preview ul { margin:4px 0 0 18px; padding:0; }
    .evidence-preview li { margin:2px 0; }
    @media (max-width:1040px) { .summary, .pipeline, .scope-header, .taxonomy-columns { grid-template-columns:repeat(2,minmax(0,1fr)); } .major-grid { grid-template-columns:repeat(3,minmax(0,1fr)); } .scroll { max-height:none; } table { min-width:1100px; } }
    @media (max-width:680px) { main { padding:12px; } .summary, .pipeline, .evidence-map, .scope-header, .taxonomy-columns { grid-template-columns:1fr; } .major-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } input.keyword { min-width:100%; } .field { width:100%; } }
  </style>
</head>
<body>
<main>
  <h1>KSA Definition Dashboard</h1>
  <p class="review-lead">대분류부터 세분류까지 클릭해서 범위를 좁힌 뒤, KSA 라벨과 정의 후보를 사람이 확인하는 화면입니다.</p>
  <section class="panel review-guide">
    <strong>검토 흐름</strong>
    <ol>
      <li>대분류, 중분류, 소분류, 세분류를 차례로 선택합니다.</li>
      <li>상단 진행률에서 해당 분류의 자동 정리, 사람 확인, 대기 건수를 확인합니다.</li>
      <li>KSA 원문과 짧은 라벨 후보를 보고 사람확인, 수정필요, 거절 중 하나를 누릅니다.</li>
    </ol>
    <span class="muted">원문 KSA는 수정하지 않습니다. 버튼은 후보 검토 상태와 감사 로그만 기록합니다.</span>
    <span class="pill warn">status_update_allowed=false</span>
  </section>
  <p class="muted">Human-review surface for raw KSA, atomic KSA preprocessing output, <b>단어형 대표 라벨 후보</b>, linked ontology concept terms, term-style definition candidates, and separate task-context evidence.</p>
  <div class="answer-box">
    <strong>Where the word-style preprocessing appears</strong>
    Use the <b>Short Label Candidate / 단어형 대표 라벨 후보</b> column. DB field:
    <code>ontology_concept_label_candidates.label_text</code>. The raw KSA remains unchanged in
    <code>ksa_items.ksa_text_raw</code>; this label is a review-only candidate, not an approved definition.
  </div>
  <div class="answer-box">
    <strong>How to read each row</strong>
    Read from left to right: <b>원문 KSA</b> is the untouched source text, <b>전처리 단계</b> shows what was extracted or linked,
    and <b>단어형 전처리 결과</b> is the compact label candidate that a human can mark as 사람확인, 수정필요, or 거절.
  </div>
  <div class="answer-box">
    <strong>Row evidence map</strong>
    <div class="evidence-map">
      <div><b>1 Raw KSA source</b><code>ksa_items.ksa_text_raw</code></div>
      <div><b>2 Atomic KSA candidate</b><code>ksa_atomic_items.atom_text</code></div>
      <div><b>3 Representative concept name</b><code>ontology_concepts.concept_name</code></div>
      <div><b>4 Short label candidate</b><code>ontology_concept_label_candidates.label_text</code></div>
      <div><b>5 Term definition candidate</b><code>ksa_meaning_candidates.meaning_text where source_method='term_definition_template'</code></div>
      <div><b>6 Task evidence links</b><code>criteria_concept_links + task_context_template</code></div>
    </div>
  </div>
  <div class="answer-box">
    <strong>Manual review packs</strong>
    <a class="button-link secondary" href="/ksa-preprocessing-pipeline-status" target="_blank" rel="noopener">Preprocessing pipeline status</a>
    <a class="button-link secondary" href="/ksa-label-needs-review-seedpack" target="_blank" rel="noopener">Label needs_review pack</a>
    <a class="button-link secondary" href="/ksa-meaning-needs-review-seedpack" target="_blank" rel="noopener">Meaning needs_review pack</a>
    <a class="button-link secondary" href="/ksa-meaning-missing-scoped-seedpack" target="_blank" rel="noopener">Meaning missing scoped pack</a>
    <span class="muted">These static packs are no-write review surfaces. They show rows the automated judge refused to promote or rows with no current-KSA scoped meaning candidate.</span>
  </div>

  <section class="panel">
    <div class="scope-header">
      <div>
        <h2>분류별 KSA 리뷰 탐색</h2>
        <p class="muted">메인 API 화면처럼 대분류부터 세분류까지 클릭하면 같은 범위의 KSA 검토 항목이 다시 조회됩니다.</p>
      </div>
      <div id="reviewScopeProgress" class="scope-progress">
        <span class="muted">분류를 선택하면 진행률이 표시됩니다.</span>
      </div>
    </div>
    <div id="ksaMajorTiles" class="major-grid"></div>
    <div class="taxonomy-columns">
      <section class="taxonomy-column">
        <header><strong>중분류</strong><span id="ksaMiddleMeta" class="muted"></span></header>
        <div id="ksaMiddleList" class="taxonomy-list"><span class="muted">대분류를 선택하세요.</span></div>
      </section>
      <section class="taxonomy-column">
        <header><strong>소분류</strong><span id="ksaSmallMeta" class="muted"></span></header>
        <div id="ksaSmallList" class="taxonomy-list"><span class="muted">중분류를 선택하세요.</span></div>
      </section>
      <section class="taxonomy-column">
        <header><strong>세분류</strong><span id="ksaSubMeta" class="muted"></span></header>
        <div id="ksaSubList" class="taxonomy-list"><span class="muted">소분류를 선택하세요.</span></div>
      </section>
    </div>
  </section>

  <section class="panel">
    <div class="toolbar">
      <div class="field"><label>대분류</label><input id="majorCode" class="code" value=""></div>
      <div class="field"><label>중분류</label><input id="middleCode" class="code" value=""></div>
      <div class="field"><label>소분류</label><input id="smallCode" class="code" value=""></div>
      <div class="field"><label>세분류</label><input id="subCode" class="code" value=""></div>
      <div class="field"><label>검색어</label><input id="keyword" class="keyword" placeholder="KSA, 능력단위, 개념명, 정의 후보"></div>
      <div class="field">
        <label>KSA 유형</label>
        <select id="conceptType">
          <option value="">전체</option>
          <option value="knowledge">지식</option>
          <option value="skill">기술</option>
          <option value="attitude">태도</option>
        </select>
      </div>
      <div class="field">
        <label>정의 상태</label>
        <select id="definitionState">
          <option value="all">전체</option>
          <option value="defined">정의 있음</option>
          <option value="missing">정의 없음</option>
          <option value="candidate">자동 후보 있음</option>
          <option value="human_reviewed">사람 확인 완료</option>
          <option value="unlinked">개념 미연결</option>
        </select>
      </div>
      <div class="field">
        <label>라벨 상태</label>
        <select id="labelState">
          <option value="all">전체</option>
          <option value="shortened">짧게 정리됨</option>
          <option value="unchanged">이미 짧음</option>
          <option value="missing">후보 없음</option>
          <option value="collision">동일 라벨 충돌</option>
          <option value="generic">너무 일반적</option>
          <option value="quality_review">품질 검토 필요</option>
        </select>
      </div>
      <div class="field">
        <label>라벨 사람확인</label>
        <select id="labelReviewStatus">
          <option value="all">전체</option>
          <option value="llm_reviewed" selected>LLM 단어형 검토됨</option>
          <option value="candidate">라벨 전처리 후보</option>
          <option value="human_reviewed">사람확인</option>
          <option value="needs_review">수정대상</option>
          <option value="rejected">거절</option>
          <option value="missing">후보 없음</option>
        </select>
      </div>
      <div class="field">
        <label>정의 후보 확인</label>
        <select id="meaningReviewStatus">
          <option value="all" selected>전체</option>
          <option value="llm_reviewed">LLM 단어형 검토됨</option>
          <option value="candidate">정의 문장 후보</option>
          <option value="needs_review">수정필요</option>
          <option value="human_reviewed">사람확인</option>
          <option value="missing">후보 없음</option>
        </select>
      </div>
      <div class="field"><label>표시 건수</label><input id="limit" class="code" value="100"></div>
      <div class="field"><label>Decision packet</label><input id="reviewSourcePacket" class="keyword" placeholder="reports/...csv#label:123:approve"></div>
      <div class="field"><label>Packet SHA-256</label><input id="reviewSourceHash" class="keyword" placeholder="sha256:..."></div>
      <input id="labelReviewerId" type="hidden" value="">
      <input id="labelReviewNote" type="hidden" value="">
      <input id="labelRawToLabelChecked" type="checkbox" checked hidden>
      <input id="meaningRawToMeaningChecked" type="checkbox" checked hidden>
      <button onclick="refreshKsaReview()">조회</button>
      <button class="secondary" onclick="setHrScope()">인사 직무</button>
      <button class="secondary" onclick="setPendingReviewQueue()">단어형 후보</button>
      <button class="secondary" onclick="setQualityReviewQueue()">품질검토</button>
      <button class="secondary" onclick="setMissingLabelQueue()">라벨 없음</button>
      <button class="secondary" onclick="setNeedsRevisionQueue()">수정필요</button>
      <button class="secondary" onclick="setMeaningNeedsReviewQueue()">정의 수정필요</button>
      <button class="secondary" onclick="setMissingMeaningQueue()">정의 없음</button>
      <button class="secondary" onclick="clearFilters()">초기화</button>
      <a class="button-link secondary" href="/ksa-preprocessing-dashboard" target="_blank" rel="noopener">전처리 현황</a>
      <a class="button-link secondary" href="/ksa-label-auto-triage">Auto Triage</a>
      <a class="button-link secondary" href="/">Main Dashboard</a>
    </div>
    <div id="status" class="status muted"></div>
    <div class="notice">이 화면은 검토자가 후보를 확인하는 곳입니다. 전처리 상세 현황과 품질 플래그 설명은 별도 <a href="/ksa-preprocessing-dashboard" target="_blank" rel="noopener">전처리 현황</a> 화면으로 분리했습니다.</div>
  </section>

  <section class="panel">
    <p class="table-note">검토 항목입니다. 전처리 상세 현황은 별도 <a href="/ksa-preprocessing-dashboard" target="_blank" rel="noopener">전처리 현황</a> 화면에서 확인합니다.</p>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th style="width:340px;">검토할 KSA<br><span class="muted">원문은 수정하지 않습니다.</span></th>
            <th style="width:320px;">짧은 라벨 검토<br><span class="muted">사람확인 / 수정필요 / 거절</span></th>
            <th style="width:330px;">정의 후보 검토<br><span class="muted">정의 승격 전 검토용 후보</span></th>
            <th style="width:380px;">근거<br><span class="muted">수행준거, 과업, 원자 KSA 근거</span></th>
            <th style="width:220px;">진행 상태</th>
          </tr>
        </thead>
        <tbody id="items">
          <tr><td colspan="5" class="muted">분류를 선택하거나 Load를 눌러 KSA 검토 항목을 불러오세요.</td></tr>
        </tbody>
      </table>
    </div>
  </section>
</main>
<script>
const q = id => document.getElementById(id);
const fmt = new Intl.NumberFormat('ko-KR');
let latestRows = [];
for (const [selector, label] of [
  ['button[onclick="loadAutoTriage()"]', 'Load'],
  ['button[onclick="clearScope()"]', 'All'],
]) {
  const button = document.querySelector(selector);
  if (button) button.textContent = label;
}
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function params() {
  const p = new URLSearchParams();
  for (const [id, key] of [
    ['majorCode', 'major_code'],
    ['middleCode', 'middle_code'],
    ['smallCode', 'small_code'],
    ['subCode', 'sub_code'],
    ['keyword', 'keyword'],
    ['conceptType', 'concept_type'],
    ['definitionState', 'definition_state'],
    ['labelState', 'label_state'],
    ['labelReviewStatus', 'label_review_status'],
    ['meaningReviewStatus', 'meaning_review_status'],
    ['limit', 'limit'],
  ]) {
    const value = q(id).value.trim();
    if (value) p.set(key, value);
  }
  return p;
}
function applyInitialQueryParams() {
  const search = new URLSearchParams(window.location.search);
  let applied = false;
  for (const [id, key] of [
    ['majorCode', 'major_code'],
    ['middleCode', 'middle_code'],
    ['smallCode', 'small_code'],
    ['subCode', 'sub_code'],
    ['keyword', 'keyword'],
    ['conceptType', 'concept_type'],
    ['definitionState', 'definition_state'],
    ['labelState', 'label_state'],
    ['labelReviewStatus', 'label_review_status'],
    ['meaningReviewStatus', 'meaning_review_status'],
    ['limit', 'limit'],
  ]) {
    if (!search.has(key)) continue;
    const control = q(id);
    if (!control) continue;
    let value = search.get(key) || '';
    if (id === 'conceptType' && value === 'all') value = '';
    if (control.tagName === 'SELECT') {
      const optionValues = Array.from(control.options).map(option => option.value);
      if (!optionValues.includes(value)) continue;
    }
    control.value = value;
    applied = true;
  }
  return applied;
}
function metric(label, value) {
  return `<div class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;
}
function metricClass(label, value, cls) {
  return `<div class="metric ${esc(cls || '')}"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;
}
function sumCountMap(values) {
  return Object.values(values || {}).reduce((total, value) => total + Number(value || 0), 0);
}
function stage(title, detail, value, cls) {
  return `<div class="stage ${esc(cls || '')}"><b>${esc(title)}</b><span class="muted">${esc(detail)}</span><strong>${esc(value)}</strong></div>`;
}
function renderCountMap(label, values) {
  const items = Object.entries(values || {});
  if (!items.length) return `<span class="pill">${esc(label)}: none</span>`;
  return `<strong>${esc(label)}</strong> ` + items.map(([key, value]) => `<span class="pill">${esc(key)} ${esc(value)}</span>`).join('');
}
function progressBar(percent) {
  const value = Math.max(0, Math.min(100, Number(percent || 0)));
  return `<div class="progress"><span style="width:${value.toFixed(1)}%"></span></div>`;
}
function selectedCodes() {
  return {
    major: q('majorCode').value.trim(),
    middle: q('middleCode').value.trim(),
    small: q('smallCode').value.trim(),
    sub: q('subCode').value.trim(),
  };
}
function setScope(major='', middle='', small='', sub='') {
  q('majorCode').value = major || '';
  q('middleCode').value = middle || '';
  q('smallCode').value = small || '';
  q('subCode').value = sub || '';
}
function ksaTaxonomyParams(level) {
  const params = new URLSearchParams();
  const codes = selectedCodes();
  params.set('level', level);
  params.set('limit', level === 'major' ? '100' : '500');
  if (codes.major) params.set('major_code', codes.major);
  if (codes.middle) params.set('middle_code', codes.middle);
  if (codes.small) params.set('small_code', codes.small);
  if (codes.sub) params.set('sub_code', codes.sub);
  return params;
}
async function fetchJson(path) {
  const response = await fetch(path);
  const data = await response.json();
  if (!response.ok || data.ok === false) throw new Error((data.error && data.error.detail) || data.error || 'request failed');
  return data;
}
function nodeName(target) {
  const el = q(target)?.querySelector('.active .node-title, .active .tile-title');
  return el ? el.textContent : '';
}
function currentScopeLabel() {
  const names = [
    nodeName('ksaMajorTiles'),
    nodeName('ksaMiddleList'),
    nodeName('ksaSmallList'),
    nodeName('ksaSubList'),
  ].filter(Boolean);
  const codes = selectedCodes();
  const codeText = [codes.major, codes.middle, codes.small, codes.sub].filter(Boolean).join('-') || '전체 NCS';
  return names.length ? `${names.join(' > ')} (${codeText})` : codeText;
}
function renderKsaMajorTiles(nodes) {
  const codes = selectedCodes();
  q('ksaMajorTiles').innerHTML = (nodes || []).map(node => {
    const active = node.major_code === codes.major;
    const pct = Number(node.element_percent || 0);
    return `<button type="button" class="major-tile${active ? ' active' : ''}" onclick="selectKsaMajor('${esc(node.major_code)}')">
      <span class="tile-title">${esc(node.major_code)}. ${esc(node.name || '')}</span>
      <span class="tile-meta">API ${pct.toFixed(1)}% · ${fmt.format(Number(node.element_matched || 0))}/${fmt.format(Number(node.element_count || 0))}</span>
      ${progressBar(pct)}
    </button>`;
  }).join('');
}
function renderKsaNodeList(target, metaTarget, nodes, level) {
  const codes = selectedCodes();
  q(metaTarget).textContent = nodes && nodes.length ? `${fmt.format(nodes.length)}개` : '';
  if (!nodes || !nodes.length) {
    const messages = {middle:'대분류를 선택하세요.', small:'중분류를 선택하세요.', sub:'소분류를 선택하세요.'};
    q(target).innerHTML = `<span class="muted">${esc(messages[level] || '결과가 없습니다.')}</span>`;
    return;
  }
  q(target).innerHTML = nodes.map(node => {
    const active =
      (level === 'middle' && node.middle_code === codes.middle) ||
      (level === 'small' && node.small_code === codes.small) ||
      (level === 'sub' && node.sub_code === codes.sub);
    const pct = Number(node.element_percent || 0);
    const click =
      level === 'middle'
        ? `selectKsaMiddle('${esc(node.major_code)}','${esc(node.middle_code)}')`
        : level === 'small'
          ? `selectKsaSmall('${esc(node.major_code)}','${esc(node.middle_code)}','${esc(node.small_code)}')`
          : `selectKsaSub('${esc(node.major_code)}','${esc(node.middle_code)}','${esc(node.small_code)}','${esc(node.sub_code)}')`;
    return `<button type="button" class="node${active ? ' active' : ''}" onclick="${click}">
      <span class="node-title">${esc(node.code)}. ${esc(node.name || '')}</span>
      <span class="node-sub">API ${pct.toFixed(1)}% · 능력단위 ${fmt.format(Number(node.unit_count || 0))}</span>
      ${progressBar(pct)}
    </button>`;
  }).join('');
}
async function loadKsaTaxonomy() {
  const codes = selectedCodes();
  const majors = await fetchJson('/api/taxonomy?' + ksaTaxonomyParams('major').toString());
  const middles = codes.major ? await fetchJson('/api/taxonomy?' + ksaTaxonomyParams('middle').toString()) : {nodes:[]};
  const smalls = codes.major && codes.middle ? await fetchJson('/api/taxonomy?' + ksaTaxonomyParams('small').toString()) : {nodes:[]};
  const subs = codes.major && codes.middle && codes.small ? await fetchJson('/api/taxonomy?' + ksaTaxonomyParams('sub').toString()) : {nodes:[]};
  renderKsaMajorTiles(majors.nodes || []);
  renderKsaNodeList('ksaMiddleList', 'ksaMiddleMeta', middles.nodes || [], 'middle');
  renderKsaNodeList('ksaSmallList', 'ksaSmallMeta', smalls.nodes || [], 'small');
  renderKsaNodeList('ksaSubList', 'ksaSubMeta', subs.nodes || [], 'sub');
}
async function selectKsaMajor(major) {
  setScope(major, '', '', '');
  await loadKsaTaxonomy();
  await loadDefinitions();
}
async function selectKsaMiddle(major, middle) {
  setScope(major, middle, '', '');
  await loadKsaTaxonomy();
  await loadDefinitions();
}
async function selectKsaSmall(major, middle, small) {
  setScope(major, middle, small, '');
  await loadKsaTaxonomy();
  await loadDefinitions();
}
async function selectKsaSub(major, middle, small, sub) {
  setScope(major, middle, small, sub);
  await loadKsaTaxonomy();
  await loadDefinitions();
}
async function refreshKsaReview() {
  await loadKsaTaxonomy();
  await loadDefinitions();
}
function renderReviewScopeProgress(data) {
  const progress = data.label_review_progress || {};
  const total = Number(progress.total || 0);
  const actioned = Number(progress.actioned || 0);
  const human = Number(progress.human_reviewed || 0);
  const pending = Number(progress.pending || 0);
  const needs = Number(progress.needs_review || 0);
  const rejected = Number(progress.rejected || 0);
  const actionedPercent = Number(progress.actioned_percent || 0);
  const humanPercent = Number(progress.human_reviewed_percent || 0);
  q('reviewScopeProgress').innerHTML = `
    <span class="muted">현재 범위</span>
    <strong>${esc(currentScopeLabel())}</strong>
    <div><b>정리 진행률</b> ${fmt.format(actioned)} / ${fmt.format(total)} (${actionedPercent.toFixed(1)}%)</div>
    ${progressBar(actionedPercent)}
    <div><b>사람 확인</b> ${fmt.format(human)} / ${fmt.format(total)} (${humanPercent.toFixed(1)}%)</div>
    ${progressBar(humanPercent)}
    <div class="muted">대기 ${fmt.format(pending)} · 수정필요 ${fmt.format(needs)} · 거절 ${fmt.format(rejected)}</div>
  `;
}
function ensureReviewInputs(kind, decision) {
  let reviewerId = q('labelReviewerId').value.trim();
  if (!reviewerId) {
    reviewerId = (window.prompt('검토자 ID를 입력하세요. 사람 검토 기록에 남습니다.') || '').trim();
    if (reviewerId) q('labelReviewerId').value = reviewerId;
  }
  let note = q('labelReviewNote').value.trim();
  if (!note) {
    note = (window.prompt('검토 근거를 짧게 입력하세요. 예: 원문과 라벨 의미 일치 확인') || '').trim();
    if (note) q('labelReviewNote').value = note;
  }
  const checkId = kind === 'meaning' ? 'meaningRawToMeaningChecked' : 'labelRawToLabelChecked';
  let checked = q(checkId).checked;
  if (!checked) {
    checked = window.confirm(kind === 'meaning'
      ? '원문 KSA, 정의 후보, 수행준거/과업 근거를 직접 확인했습니까?'
      : '원문 KSA, 원자 KSA, 대표 개념, 짧은 라벨 후보를 직접 확인했습니까?');
    q(checkId).checked = checked;
  }
  if (!reviewerId || !note || !checked) {
    q('status').className = 'status error';
    q('status').textContent = '사람 검토 기록에는 검토자 ID, 근거 메모, 확인 체크가 모두 필요합니다.';
    return null;
  }
  if (!window.confirm(`${decision} 상태로 저장할까요? 원문 KSA는 수정되지 않고 후보 검토 상태만 기록됩니다.`)) {
    q('status').className = 'status muted';
    q('status').textContent = '저장을 취소했습니다.';
    return null;
  }
  let sourcePacket = '';
  let sourceHash = '';
  if (decision === 'approve') {
    sourcePacket = q('reviewSourcePacket').value.trim();
    if (!sourcePacket) {
      sourcePacket = (window.prompt('Packet-backed source_decision_packet path/ref를 입력하세요. 예: reports/review.csv#label:123:approve') || '').trim();
      if (sourcePacket) q('reviewSourcePacket').value = sourcePacket;
    }
    sourceHash = q('reviewSourceHash').value.trim();
    if (!sourceHash) {
      sourceHash = (window.prompt('해당 packet artifact 파일의 SHA-256을 sha256:<64hex> 형식으로 입력하세요.') || '').trim();
      if (sourceHash) q('reviewSourceHash').value = sourceHash;
    }
    if (!sourcePacket || !sourceHash) {
      q('status').className = 'status error';
      q('status').textContent = '사람확인에는 기존 reports 패킷 참조와 그 파일 SHA-256이 필요합니다.';
      return null;
    }
  }
  return {reviewerId, note, checked, sourcePacket, sourceHash};
}
function hasScopedMeaningEvidence(item) {
  return Boolean(
    item
    && item.ksa_id
    && (
      item.unit_code
      || item.element_id
      || item.criteria_ids?.length
      || item.related_criteria_count
      || item.task_evidence_count
    )
  );
}
function labelReviewStatusText(status) {
  return ({
    llm_reviewed: 'LLM 단어형 검토됨 · 자동 추출 결과(미승인)',
    candidate: '단어형 라벨 후보(candidate)',
    needs_review: '수정필요',
    human_reviewed: '사람확인',
    rejected: '거절',
    accepted: '사람확인',
    reviewed: '사람확인',
    missing: '후보 없음',
  })[status || 'missing'] || status || 'missing';
}
function meaningReviewStatusText(status) {
  return ({
    candidate: 'LLM 전처리됨 · 자동 추출 결과(미승인) · 사람확인 대기',
    llm_reviewed: 'LLM 단어형 검토됨 · 자동 추출 결과(미승인)',
    needs_review: '수정필요',
    human_reviewed: '사람확인',
    rejected: '거절',
    accepted: '사람확인',
    reviewed: '사람확인',
    missing: '후보 없음',
  })[status || 'missing'] || status || 'missing';
}
function renderRows(items) {
  if (!items.length) {
    q('items').innerHTML = '<tr><td colspan="5" class="muted">No matching KSA definition rows. Check Label State / 단어형 라벨 상태 if you are looking for shortened candidates.</td></tr>';
    return;
  }
  q('items').innerHTML = items.map(item => {
    const kind = item.definition_kind || 'missing_definition';
    const stageClass = kind === 'human_reviewed_definition' ? 'good' : (kind === 'legacy_task_context_in_definition' || kind === 'model_candidate_definition' || kind === 'model_term_definition_candidate' ? 'warn' : '');
    const definition = item.definition
      ? `<span class="pill ${stageClass}">${esc(kind)}</span><div class="definition">${esc(item.definition)}</div>`
      : '<span class="empty-definition">missing</span>';
    const concept = item.concept_id
      ? `<strong>${esc(item.concept_name)}</strong><br><span class="muted">${esc(item.alias_sample || '')}</span>`
      : '<span class="empty-definition">unlinked</span>';
    const atomic = item.atomic_ksa_sample
      ? `<strong>${esc(item.atomic_ksa_sample)}</strong><br><span class="muted">count ${esc(item.atomic_ksa_count || 0)} / ${esc(item.atomic_split_methods || '')}</span><br><span class="muted">atomic concept links ${esc(item.atomic_concept_link_count || 0)}</span>`
      : '<span class="empty-definition">not preprocessed</span>';
    const labelComparison = item.short_label_candidate
      ? `<br><span class="muted">transform state ${esc(item.short_label_transform_state || '')}: source ${esc(item.short_label_source_length ?? '')} chars -&gt; label ${esc(item.short_label_label_length ?? '')} chars / removed ${esc(item.short_label_removed_char_count ?? '')} / length ratio ${esc(item.short_label_length_ratio ?? '')}</span>`
      : '';
    const labelStatusText = labelReviewStatusText(item.short_label_review_status);
    const termDefinitionStatusText = meaningReviewStatusText(item.term_definition_review_status);
    const shortLabel = item.short_label_candidate
      ? `<strong>${esc(item.short_label_candidate)}</strong><br><span class="muted">${esc(item.short_label_source_method || '')} / confidence ${esc(item.short_label_confidence ?? '')} / ${esc(labelStatusText)}</span><br><span class="muted">candidate count ${esc(item.short_label_candidate_count || 0)}</span>${labelComparison}${item.short_label_quality_flags?.length ? `<br><span class="pill warn">quality review</span> <span class="muted">${esc(item.short_label_quality_flags.join(', '))}</span>` : ''}`
      : '<span class="empty-definition">단어형 대표 라벨 후보 없음</span>';
    const reviewActions = item.short_label_id
      ? `<div class="review-actions">
          <button class="good" onclick="reviewShortLabel(${Number(item.short_label_id)}, 'approve')">사람확인</button>
          <button class="secondary" onclick="reviewShortLabel(${Number(item.short_label_id)}, 'needs_revision')">수정필요</button>
          <button class="bad" onclick="reviewShortLabel(${Number(item.short_label_id)}, 'reject')">거절</button>
        </div>`
      : '';
    const reviewAudit = item.short_label_id
      ? (item.short_label_last_reviewer_id
          ? `<div class="review-audit">
              <span class="pill good">Latest label review audit</span>
              <span class="muted">reviewer ${esc(item.short_label_last_reviewer_id)} / ${esc(item.short_label_last_review_status || '')} / ${esc(item.short_label_last_reviewed_at || '')}</span>
              <span class="muted">action ${esc(item.short_label_last_review_action || '')}</span>
              ${item.short_label_last_review_note ? `<span class="muted">note: ${esc(item.short_label_last_review_note)}</span>` : ''}
              ${item.short_label_last_review_rationale ? `<span class="muted">rationale: ${esc(item.short_label_last_review_rationale)}</span>` : ''}
            </div>`
          : `<div class="review-audit"><span class="pill warn">No label review audit yet</span><span class="muted">사람확인, 수정필요, 거절 중 하나를 누르면 reviewer/note/time이 여기에 표시됩니다.</span></div>`)
      : '';
    const priorityClass = item.short_label_review_priority === 'high'
      ? 'bad'
      : (item.short_label_review_priority === 'medium' || item.short_label_review_priority === 'machine_reviewed' ? 'warn' : (item.short_label_review_priority === 'completed' ? 'good' : ''));
    const shortLabelColumn = item.short_label_candidate
      ? `<div class="label-card">
          <span class="pill warn">검토 후보</span>
          <strong>${esc(item.short_label_candidate)}</strong>
          <span class="muted">상태 ${esc(labelStatusText)}</span><br>
          <span class="pill ${priorityClass}">review priority ${esc(item.short_label_review_priority || '')}</span><span class="muted">${esc(item.short_label_review_reason || '')}</span><br>
          ${item.short_label_quality_flags?.length ? `<br><span class="pill warn">quality review</span><br><span class="muted">${esc(item.short_label_quality_flags.join(', '))}</span>` : ''}
          ${reviewActions}
          ${reviewAudit}
          <details class="review-details">
            <summary>라벨 근거 보기</summary>
            <span class="muted">label_id ${esc(item.short_label_id || '')} / ${esc(item.short_label_source_method || '')}</span><br>
            <span class="muted">source text: ${esc(item.short_label_source_text || '')}</span><br>
            <span class="muted">provenance: ${esc(item.short_label_provenance_match || '')}; source_ksa_id ${esc(item.short_label_source_ksa_id ?? '')}; source_atomic_id ${esc(item.short_label_source_atomic_id ?? '')}</span><br>
            <span class="muted">comparison: ${esc(item.short_label_transform_state || '')}; ${esc(item.short_label_source_length ?? '')} chars -&gt; ${esc(item.short_label_label_length ?? '')} chars; removed ${esc(item.short_label_removed_char_count ?? '')}; ratio ${esc(item.short_label_length_ratio ?? '')}</span><br>
            <span class="muted">scope: ${esc(item.short_label_source_scope_key || '')}</span>
          </details>
        </div>`
      : '<span class="empty-definition">단어형 대표 라벨 후보 없음</span>';
    const scopeContext = `<div class="source-block">
      <div><strong>#${esc(item.ksa_id)}</strong> ${esc(item.ksa_type_name)} ${esc(item.ksa_no)}</div>
      <div><span class="muted">${esc(item.major_code)} ${esc(item.major_name)} / ${esc(item.middle_name)} &gt; ${esc(item.small_name)} &gt; ${esc(item.sub_name)}</span></div>
      <div><strong>${esc(item.unit_code)}</strong> ${esc(item.unit_name_raw)}<br><span class="muted">${esc(item.element_no)}. ${esc(item.element_name_raw)}</span></div>
    </div>`;
    const sameAsDefinition = item.meaning_candidate && item.definition && String(item.meaning_candidate).trim() === String(item.definition).trim();
    const meaning = item.meaning_candidate
      ? (sameAsDefinition
          ? `<span class="muted">Same text as current definition. Re-run term-definition preprocessing to split this into a concise definition plus task-context evidence.</span><br><span class="muted">${esc(item.meaning_role)} / ${esc(item.meaning_source_method)} / ${esc(item.meaning_review_status)}</span>`
          : `${esc(item.meaning_candidate)}<br><span class="muted">${esc(item.meaning_role)} / ${esc(item.meaning_source_method)} / ${esc(item.meaning_review_status)}</span>`)
      : '<span class="muted">none</span>';
    const meaningHasScope = hasScopedMeaningEvidence(item);
    const unscopedMeaningWarning = item.term_definition_meaning_id && !meaningHasScope
      ? `<div class="review-audit">
          <span class="pill warn">Scoped evidence warning</span>
          <span class="muted">연결 근거가 약한 후보입니다. 버튼은 막지 않고 사람 판단으로 처리합니다.</span>
        </div>`
      : '';
    const termDefinitionApproveButton = `<button class="good" onclick="reviewMeaningCandidate(${Number(item.term_definition_meaning_id)}, 'approve')">정의 사람확인</button>`;
    const termDefinitionReviewActions = item.term_definition_meaning_id
      ? `<div class="review-actions">
          ${termDefinitionApproveButton}
          <button class="secondary" onclick="reviewMeaningCandidate(${Number(item.term_definition_meaning_id)}, 'needs_revision')">정의 수정필요</button>
          <button class="bad" onclick="reviewMeaningCandidate(${Number(item.term_definition_meaning_id)}, 'reject')">정의 거절</button>
        </div>`
      : '';
    const termDefinitionReviewAudit = item.term_definition_meaning_id
      ? (item.term_definition_last_reviewer_id
          ? `<div class="review-audit">
              <span class="pill good">Latest meaning review audit</span>
              <span class="muted">reviewer ${esc(item.term_definition_last_reviewer_id)} / ${esc(item.term_definition_last_review_status || '')} / ${esc(item.term_definition_last_reviewed_at || '')}</span>
              <span class="muted">action ${esc(item.term_definition_last_review_action || '')}</span>
              ${item.term_definition_last_review_note ? `<span class="muted">note: ${esc(item.term_definition_last_review_note)}</span>` : ''}
              ${item.term_definition_last_review_rationale ? `<span class="muted">rationale: ${esc(item.term_definition_last_review_rationale)}</span>` : ''}
            </div>`
          : `<div class="review-audit"><span class="pill warn">No meaning review audit yet</span><span class="muted">정의 사람확인, 수정필요, 거절 중 하나를 누르면 reviewer/note/time이 여기에 표시됩니다.</span></div>`)
      : '';
    const termDefinitionCandidate = item.term_definition_candidate
      ? `<div class="definition">${esc(item.term_definition_candidate)}</div><span class="muted">meaning_id ${esc(item.term_definition_meaning_id || '')} / ${esc(item.term_definition_role || 'term_definition_candidate')} / ksa_meaning_candidates.meaning_text where source_method='term_definition_template' / ${esc(termDefinitionStatusText)} / confidence ${esc(item.term_definition_confidence ?? '')}</span>${item.term_definition_evidence ? `<br><span class="muted">evidence: ${esc(item.term_definition_evidence)}</span>` : ''}${unscopedMeaningWarning}${termDefinitionReviewActions}${termDefinitionReviewAudit}`
      : '<span class="muted">none</span>';
    const criteriaTextPreview = Array.isArray(item.criteria_text_preview) && item.criteria_text_preview.length
      ? `<div><b>Criteria text preview</b><ul>${item.criteria_text_preview.map(text => `<li>${esc(text)}</li>`).join('')}</ul></div>`
      : '<div><b>Criteria text preview</b><br><span class="muted">none</span></div>';
    const taskEvidencePreview = Array.isArray(item.task_evidence_preview) && item.task_evidence_preview.length
      ? `<div><b>Task evidence preview</b><ul>${item.task_evidence_preview.map(text => `<li>${esc(text)}</li>`).join('')}</ul></div>`
      : '<div><b>Task evidence preview</b><br><span class="muted">none</span></div>';
    const taskEvidenceRefs = Array.isArray(item.task_evidence_refs) && item.task_evidence_refs.length
      ? `<div><b>Evidence refs</b><br><span class="muted">${esc(item.task_evidence_refs.join(' | '))}</span></div>`
      : '<div><b>Evidence refs</b><br><span class="muted">none</span></div>';
    const taskEvidenceBlock = `<div class="evidence-preview">
      <span class="muted">task evidence ${esc(item.task_evidence_count || 0)} / criteria links ${esc(item.related_criteria_count || 0)} / criteria ids ${esc((item.criteria_ids || []).join(', '))}</span>
      ${criteriaTextPreview}
      ${taskEvidencePreview}
      ${taskEvidenceRefs}
    </div>`;
    const atomicLabelCandidateBlock = Array.isArray(item.atomic_label_candidates) && item.atomic_label_candidates.length
      ? `<div class="evidence-preview"><b>Atomic concept/label candidates</b><ul>${item.atomic_label_candidates.map(candidate => `<li><span class="muted">#${esc(candidate.atom_index)} ${esc(candidate.split_method || '')}</span> ${esc(candidate.atom_text || '')}<br><span class="muted">concept:</span> ${esc(candidate.concept_name || 'missing')} <span class="muted">label:</span> ${esc(candidate.label_text || 'missing')}</li>`).join('')}</ul></div>`
      : '<div class="evidence-preview"><b>Atomic concept/label candidates</b><br><span class="muted">none</span></div>';
    const rawSourceColumn = `<div class="raw-card">
      <div><span class="pill">원문 KSA / ksa_items.ksa_text_raw</span><strong>${esc(item.ksa_text_raw)}</strong></div>
      ${item.ksa_text_refined ? `<div><span class="pill warn">manual refined text</span><span class="muted">${esc(item.ksa_text_refined)}</span></div>` : ''}
      ${scopeContext}
    </div>`;
    const rowEvidenceSummary = `<div class="chain-step warn">
      <span class="pill warn">Row evidence summary</span>
      <div><b>Raw</b>: ${esc(item.ksa_text_raw || 'missing')}</div>
      <div><b>Atomic</b>: ${esc(item.atomic_ksa_sample || 'not preprocessed')}</div>
      <div><b>Concept</b>: ${esc(item.concept_name || 'unlinked')}</div>
      <div><b>Short Label</b>: ${esc(item.short_label_candidate || 'missing')}</div>
      <div><b>Task Evidence</b>: ${esc(item.task_evidence_count || 0)}</div>
    </div>`;
    const preprocessColumn = `<div class="chain">
      ${rowEvidenceSummary}
      <div class="chain-step good"><span class="pill">1. Raw KSA 보존</span><div class="muted">원문은 수정하지 않고 별도 전처리 테이블에 후보만 저장</div></div>
      <div class="chain-step good"><span class="pill good">2. Atomic KSA</span>${atomic}${atomicLabelCandidateBlock}</div>
      <div class="chain-step good"><span class="pill good">3. Representative Concept</span>${concept}</div>
      <div class="chain-step warn"><span class="pill warn">4. Short Label Candidate / 단어형 대표 라벨 후보</span>${shortLabel}</div>
      <div class="chain-step warn"><span class="pill warn">5. Stored Definition + Term Definition Candidate</span><br><span class="muted">current stored value: ontology_concepts.definition</span>${definition}<br><span class="muted">${esc(item.definition_source || '')}</span><br><span class="muted">review candidate: ksa_meaning_candidates.meaning_text where source_method='term_definition_template'</span>${termDefinitionCandidate}</div>
      <div class="chain-step"><span class="pill">6. Criteria/Task Evidence Links</span><br>${taskEvidenceBlock}<br>${meaning}</div>
    </div>`;
    const ontologyPayload = item.concept_id
      ? `<div class="source-block">
          <div><span class="pill good">ontology_concepts.concept_name</span><br><strong>${esc(item.concept_name)}</strong></div>
          <div><span class="pill warn">review-only label candidate table</span><br><span class="muted">ontology_concept_label_candidates.label_text</span><br>${item.short_label_candidate ? `<strong>${esc(item.short_label_candidate)}</strong>` : '<span class="empty-definition">not generated</span>'}<br><span class="muted">candidate only; not applied to concept_name</span></div>
          <div><span class="pill warn">ontology_concepts.definition</span><br>${item.definition ? `<div class="definition">${esc(item.definition)}</div>` : '<span class="empty-definition">missing</span>'}</div>
          <div><span class="pill warn">term definition candidate source</span><br><span class="muted">ksa_meaning_candidates.meaning_text where source_method='term_definition_template'</span><br>${item.term_definition_candidate ? `<div class="definition">${esc(item.term_definition_candidate)}</div>` : '<span class="empty-definition">not generated</span>'}</div>
          <div><span class="pill">criteria_concept_links + task evidence</span><br><span class="muted">criteria links ${esc(item.related_criteria_count || 0)} / task evidence ${esc(item.task_evidence_count || 0)}</span><br>${taskEvidenceBlock}</div>
        </div>`
      : '<span class="empty-definition">No ontology concept linked to this KSA.</span>';
    return `<tr>
      <td>${rawSourceColumn}</td>
      <td>${shortLabelColumn}</td>
      <td>${termDefinitionCandidate}</td>
      <td>
        ${taskEvidenceBlock}
        <details class="review-details">
          <summary>기술 세부정보 보기</summary>
          ${preprocessColumn}
          ${ontologyPayload}
        </details>
      </td>
      <td>
        <div class="source-block">
          <strong>Label review</strong><br>
          <span class="pill">label ${esc(labelStatusText)}</span>
          <span class="pill ${priorityClass}">priority ${esc(item.short_label_review_priority || 'missing')}</span>
        </div>
        <div class="source-block">
          <strong>Concept review</strong><br>
          <span class="pill">${esc(item.concept_review_status)}</span>
          <span class="pill">link ${esc(item.link_status || 'none')}</span>
        </div>
        <div class="source-block">
          <strong>Definition status</strong><br>
          <span class="pill ${stageClass}">${esc(item.preprocessing_stage || '')}</span>
          <span class="pill">${esc(termDefinitionStatusText)}</span>
          <span class="muted">criteria ${esc(item.related_criteria_count || 0)}</span>
        </div>
      </td>
    </tr>`;
  }).join('');
}
async function reviewShortLabel(labelId, decision) {
  const reviewInput = ensureReviewInputs('label', decision);
  if (!reviewInput) return;
  q('status').className = 'status muted';
  q('status').textContent = 'Saving label review...';
  try {
    const response = await fetch('/api/ksa-label-review', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        label_id: labelId,
        decision,
        reviewer_id: reviewInput.reviewerId,
        notes: reviewInput.note,
        rationale: reviewInput.note,
        source_decision_packet: reviewInput.sourcePacket,
        source_artifact_hash: reviewInput.sourceHash,
        run_artifact: '/ksa-definitions',
        raw_to_label_checked: reviewInput.checked,
      }),
    });
    const data = await response.json();
    if (!data.ok) throw new Error(data.error || (data.blockers || []).join(', ') || 'label review failed');
    q('status').textContent = `Saved label ${data.label_id}: ${data.previous_status} -> ${data.new_status}`;
    await loadDefinitions();
  } catch (err) {
    q('status').className = 'status error';
    q('status').textContent = err.message;
  }
}
async function reviewMeaningCandidate(meaningId, decision) {
  const reviewInput = ensureReviewInputs('meaning', decision);
  if (!reviewInput) return;
  q('status').className = 'status muted';
  q('status').textContent = 'Saving meaning review...';
  try {
    const response = await fetch('/api/ksa-meaning-review', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        meaning_id: meaningId,
        decision,
        reviewer_id: reviewInput.reviewerId,
        notes: reviewInput.note,
        rationale: reviewInput.note,
        source_decision_packet: reviewInput.sourcePacket,
        source_artifact_hash: reviewInput.sourceHash,
        run_artifact: '/ksa-definitions',
        raw_to_meaning_checked: reviewInput.checked,
      }),
    });
    const data = await response.json();
    if (!data.ok) throw new Error(data.error || (data.blockers || []).join(', ') || 'meaning review failed');
    q('status').textContent = `Saved meaning ${data.meaning_id}: ${data.previous_status} -> ${data.new_status}`;
    await loadDefinitions();
  } catch (err) {
    q('status').className = 'status error';
    q('status').textContent = err.message;
  }
}
async function loadDefinitions() {
  q('status').className = 'status muted';
  q('status').textContent = 'Loading...';
  try {
    const response = await fetch('/api/ksa-definitions?' + params().toString());
    const data = await response.json();
    if (!data.ok) throw new Error((data.error && data.error.detail) || 'KSA definition dashboard failed');
    const s = data.summary || {};
    const labelCounts = data.label_review_status_counts || {};
    const labelProgress = data.label_review_progress || {};
    const labelReviewTotal = Number(labelProgress.total ?? sumCountMap(labelCounts));
    const labelHumanReviewed = Number(labelProgress.human_reviewed ?? (Number(labelCounts.human_reviewed || 0) + Number(labelCounts.accepted || 0) + Number(labelCounts.reviewed || 0)));
    const labelLlmReviewed = Number(labelProgress.llm_reviewed ?? labelCounts.llm_reviewed ?? 0);
    const labelChecked = Number(labelProgress.checked ?? (labelHumanReviewed + labelLlmReviewed));
    const labelPending = Number(labelProgress.pending ?? labelCounts.candidate ?? 0);
    const labelNeedsReview = Number(labelProgress.needs_review ?? labelCounts.needs_review ?? 0);
    const labelRejected = Number(labelProgress.rejected ?? labelCounts.rejected ?? 0);
    const labelMissing = Number(labelProgress.missing ?? labelCounts.missing ?? 0);
    const labelAutomatedActioned = Number(labelProgress.automated_actioned ?? (labelLlmReviewed + labelNeedsReview));
    const labelActioned = Number(labelProgress.actioned ?? (labelChecked + labelNeedsReview + labelRejected));
    const labelActionedPercent = Number(labelProgress.actioned_percent ?? labelProgress.coverage_percent ?? (labelReviewTotal ? labelActioned * 100 / labelReviewTotal : 0));
    const labelHumanReviewedPercent = Number(labelProgress.human_reviewed_percent ?? (labelReviewTotal ? labelHumanReviewed * 100 / labelReviewTotal : 0));
    const labelActionedCoverage = `${labelActioned}/${labelReviewTotal} (${labelActionedPercent.toFixed(1)}%)`;
    const labelHumanReviewCoverage = `${labelHumanReviewed}/${labelReviewTotal} (${labelHumanReviewedPercent.toFixed(1)}%)`;
    const labelProgressUnit = labelProgress.unit_label || labelProgress.unit || 'Filtered KSA rows';
    const qualityReviewCount = s.quality_review_label_candidate_concepts ?? 'scope filter required';
    renderReviewScopeProgress(data);
    if (q('pipeline')) q('pipeline').innerHTML = [
      stage('1. Raw KSA', 'ksa_items.ksa_text_raw', s.matching_ksa || 0, 'good'),
      stage('2. Atomic KSA', 'ksa_atomic_items.atom_text', s.atomic_preprocessed_ksa || 0, 'good'),
      stage('3. Representative Concept', 'ontology_concepts.concept_name', s.linked_ksa || 0, s.unlinked_ksa ? 'warn' : 'good'),
      stage('4. Short Label Candidate / 단어형 대표 라벨 후보', 'ontology_concept_label_candidates.label_text', s.label_candidate_concepts || 0, (s.label_candidate_concepts || 0) ? 'warn' : 'bad'),
      stage('5. Term Definition Candidate', 'ontology_concepts.definition', s.candidate_definition_concepts || 0, 'warn'),
      stage('6. Criteria/Task Evidence Links', 'criteria_concept_links + task_context_template', s.criteria_evidence_linked_ksa || s.task_context_evidence_concepts || 0, 'good'),
    ].join('');
    if (q('summary')) q('summary').innerHTML = [
      metricClass('human checked labels', labelHumanReviewed, labelHumanReviewed ? 'good' : ''),
      metricClass('llm reviewed labels', labelLlmReviewed, labelLlmReviewed ? 'good' : ''),
      metricClass('automated triage actioned labels', labelAutomatedActioned, labelAutomatedActioned ? 'good' : ''),
      metricClass('label triage progress', labelActionedCoverage, labelPending ? 'warn' : 'good'),
      metricClass('human review progress', labelHumanReviewCoverage, labelHumanReviewed ? 'good' : 'warn'),
      metric('label progress unit / 집계 단위', labelProgressUnit),
      metricClass('pending labels / 미확인 단어형 후보', labelPending, labelPending ? 'warn' : 'good'),
      metricClass('needs revision labels / 수정필요', labelNeedsReview, labelNeedsReview ? 'bad' : 'good'),
      metricClass('rejected labels / 거절', labelRejected, labelRejected ? 'warn' : 'good'),
      metricClass('missing labels / 후보 없음', labelMissing, labelMissing ? 'bad' : 'good'),
      metric('distinct concepts', s.concepts),
      metricClass('atomic KSA generated', s.atomic_preprocessed_ksa, 'good'),
      metricClass('atomic concept linked', s.atomic_concept_linked_ksa, 'good'),
      metricClass('short label candidate concepts / 단어형 라벨 후보', s.label_candidate_concepts, 'warn'),
      metricClass('shortened label candidates / 단어형 압축 후보', s.shortened_label_candidate_concepts, 'warn'),
      metricClass('unchanged label candidates / 이미 짧음', s.unchanged_label_candidate_concepts, 'warn'),
      metricClass('missing label candidates / 후보 없음', s.missing_label_candidate_concepts, s.missing_label_candidate_concepts ? 'bad' : 'good'),
      metricClass('collision label candidates / 동일 라벨 충돌', s.collision_label_candidate_concepts, s.collision_label_candidate_concepts ? 'bad' : 'good'),
      metricClass('generic label candidates / 과도하게 일반적', s.generic_label_candidate_concepts, s.generic_label_candidate_concepts ? 'bad' : 'good'),
      metricClass('quality review label candidates / 품질 검토 필요', qualityReviewCount, s.quality_review_label_candidate_concepts ? 'bad' : 'good'),
      metricClass('source-missing label anomalies / 출처 없음', s.provenance_missing_label_candidate_concepts, s.provenance_missing_label_candidate_concepts ? 'bad' : 'good'),
      metricClass('human-confirmed label anomalies / human approval should be 0', s.human_confirmed_label_candidate_anomalies, s.human_confirmed_label_candidate_anomalies ? 'bad' : 'good'),
      metricClass('llm reviewed label candidates / review context only', s.llm_reviewed_label_concepts, s.llm_reviewed_label_concepts ? 'warn' : ''),
      metricClass('llm-or-human reviewed label candidates / not approval', s.llm_or_human_reviewed_label_candidate_concepts, s.llm_or_human_reviewed_label_candidate_concepts ? 'warn' : ''),
      metricClass('llm reviewed meaning concepts / automatic only', s.llm_reviewed_meaning_concepts, s.llm_reviewed_meaning_concepts ? 'warn' : ''),
      metricClass('needs review meaning concepts', s.needs_review_meaning_concepts, s.needs_review_meaning_concepts ? 'bad' : 'good'),
      metricClass('LLM/rule preprocessed meaning candidates', s.candidate_meaning_concepts, s.candidate_meaning_concepts ? 'warn' : 'good'),
      metricClass('text in definition field', s.concepts_with_definition_text, 'warn'),
      metricClass('definition_status=defined', s.defined_concepts, 'good'),
      metricClass('definition_status=candidate', s.candidate_definition_concepts, 'warn'),
      metricClass('review_status=model_preprocessed', s.model_preprocessed_concepts, 'warn'),
      metricClass('review_status=human_reviewed', s.human_reviewed_concepts, 'good'),
      metric('task-context evidence concepts', s.task_context_evidence_concepts),
      metric('human reviewed definitions', s.human_reviewed_definition_concepts),
      metric('missing definition concepts', s.missing_definition_concepts),
      metric('unlinked KSA', s.unlinked_ksa),
      metric('displayed rows', (data.items || []).length),
    ].join('');
    if (q('counts')) q('counts').innerHTML = [
      renderCountMap('definition_status', data.definition_status_counts),
      renderCountMap('concept_review_status', data.concept_review_status_counts),
      renderCountMap('label_review_status', data.label_review_status_counts),
      renderCountMap('meaning_review_status', data.meaning_review_status_counts),
      renderCountMap('label_quality_flags', data.label_quality_flag_counts),
    ].join('<br>');
    renderRows(data.items || []);
    q('status').textContent = `Loaded ${(data.items || []).length} rows from ${s.matching_ksa || 0} matching KSA records.`;
  } catch (err) {
    q('status').className = 'status error';
    q('status').textContent = err.message;
  }
}
async function setHrScope() {
  q('majorCode').value = '02';
  q('middleCode').value = '02';
  q('smallCode').value = '02';
  q('subCode').value = '01';
  await loadKsaTaxonomy();
  await loadDefinitions();
}
function ensureReviewQueueScope() {
  const scopeIds = ['majorCode', 'middleCode', 'smallCode', 'subCode', 'keyword'];
  const hasScope = scopeIds.some(id => {
    const value = q(id).value.trim();
    return value && value !== 'all';
  });
  if (!hasScope) {
    q('majorCode').value = '02';
  }
}
async function setPendingReviewQueue() {
  ensureReviewQueueScope();
  q('labelState').value = 'all';
  q('labelReviewStatus').value = 'llm_reviewed';
  q('meaningReviewStatus').value = 'all';
  q('limit').value = '100';
  await loadKsaTaxonomy();
  await loadDefinitions();
}
async function setQualityReviewQueue() {
  ensureReviewQueueScope();
  q('labelState').value = 'quality_review';
  q('labelReviewStatus').value = 'all';
  q('meaningReviewStatus').value = 'all';
  q('limit').value = '100';
  await loadKsaTaxonomy();
  await loadDefinitions();
}
async function setMissingLabelQueue() {
  ensureReviewQueueScope();
  q('labelState').value = 'missing';
  q('labelReviewStatus').value = 'all';
  q('meaningReviewStatus').value = 'all';
  q('limit').value = '100';
  await loadKsaTaxonomy();
  await loadDefinitions();
}
async function setNeedsRevisionQueue() {
  ensureReviewQueueScope();
  q('labelState').value = 'all';
  q('labelReviewStatus').value = 'needs_review';
  q('meaningReviewStatus').value = 'all';
  q('limit').value = '100';
  await loadKsaTaxonomy();
  await loadDefinitions();
}
async function setMeaningNeedsReviewQueue() {
  ensureReviewQueueScope();
  q('labelState').value = 'all';
  q('labelReviewStatus').value = 'all';
  q('meaningReviewStatus').value = 'needs_review';
  q('limit').value = '100';
  await loadKsaTaxonomy();
  await loadDefinitions();
}
async function setMissingMeaningQueue() {
  ensureReviewQueueScope();
  q('labelState').value = 'all';
  q('labelReviewStatus').value = 'all';
  q('meaningReviewStatus').value = 'missing';
  q('limit').value = '100';
  await loadKsaTaxonomy();
  await loadDefinitions();
}
async function clearFilters() {
  for (const id of ['majorCode', 'middleCode', 'smallCode', 'subCode', 'keyword', 'conceptType']) q(id).value = '';
  q('definitionState').value = 'all';
  q('labelState').value = 'all';
  q('labelReviewStatus').value = 'all';
  q('meaningReviewStatus').value = 'all';
  q('limit').value = '100';
  await loadKsaTaxonomy();
  await loadDefinitions();
}
applyInitialQueryParams();
loadKsaTaxonomy().finally(loadDefinitions);
</script>
</body>
</html>
"""


def render_ksa_label_patterns_html() -> str:
    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KSA Label Pattern Classifier</title>
  <style>
    :root { --accent:#0f766e; --line:#d1d5db; --muted:#64748b; --good:#15803d; --warn:#b45309; --bad:#b91c1c; --bg:#f8fafc; }
    body { margin:0; font-family:system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--bg); color:#0f172a; }
    main { padding:20px; max-width:1440px; margin:0 auto; display:grid; gap:16px; }
    h1, h2 { margin:0; }
    .muted { color:var(--muted); }
    .topbar { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }
    .panel { background:#fff; border:1px solid var(--line); border-radius:8px; padding:14px; box-shadow:0 1px 2px rgba(15,23,42,.05); }
    .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:flex-end; }
    .field { display:grid; gap:4px; }
    label { font-size:12px; font-weight:700; color:#475569; }
    input, select { height:36px; border:1px solid #cbd5e1; border-radius:6px; padding:0 9px; font-size:14px; background:#fff; }
    input.code { width:92px; }
    button, a.button-link { min-height:36px; border:1px solid var(--accent); border-radius:6px; background:var(--accent); color:#fff; padding:8px 12px; font-weight:700; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; gap:6px; }
    button.secondary, a.button-link.secondary { background:#fff; color:var(--accent); }
    .summary { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:10px; background:#fff; display:grid; gap:4px; }
    .metric span { color:var(--muted); font-size:12px; }
    .metric strong { font-size:22px; }
    .notice { border-left:4px solid var(--accent); background:#ecfdf5; padding:10px 12px; display:grid; gap:4px; }
    .scroll { overflow:auto; border:1px solid var(--line); border-radius:8px; }
    table { width:100%; border-collapse:collapse; min-width:1120px; }
    th, td { border-bottom:1px solid #e5e7eb; padding:9px 10px; text-align:left; vertical-align:top; }
    th { background:#f1f5f9; font-size:12px; color:#334155; }
    .pill { display:inline-flex; align-items:center; border-radius:999px; padding:3px 8px; font-size:12px; font-weight:700; background:#e2e8f0; color:#334155; margin:1px; }
    .pill.good { background:#dcfce7; color:#166534; }
    .pill.warn { background:#fef3c7; color:#92400e; }
    .pill.bad { background:#fee2e2; color:#991b1b; }
    .group-row { cursor:pointer; }
    .group-row:hover { background:#f8fafc; }
    .samples { display:grid; gap:8px; }
    .sample { border:1px solid #e2e8f0; border-radius:8px; padding:9px; background:#fff; display:grid; gap:5px; }
    .sample strong { font-size:14px; }
    .sample code { color:#475569; white-space:normal; }
    @media (max-width:900px) { main { padding:12px; } .topbar { display:grid; } .summary { grid-template-columns:1fr; } .field, button, a.button-link { width:100%; } input.code { width:100%; } }
  </style>
</head>
<body>
<main>
  <header class="topbar">
    <div>
      <h1>KSA 라벨 유형 분류</h1>
      <p class="muted">01 대분류처럼 사람이 먼저 본 범위를 기준으로 전체 단어형 KSA 라벨 후보를 묶어 보는 읽기 전용 화면입니다.</p>
    </div>
    <div style="display:flex; gap:8px; flex-wrap:wrap;">
      <a class="button-link secondary" href="/ksa-label-auto-triage">Auto Triage</a>
      <a class="button-link secondary" href="/ksa-review-dashboard">라벨 리뷰 화면</a>
      <a class="button-link secondary" href="/">Main Dashboard</a>
    </div>
  </header>

  <section class="panel notice">
    <strong>운영 원칙</strong>
    <span>이 화면은 자동 승인 화면이 아닙니다. 01에서 사람이 확인한 패턴을 전체 후보에 대입해 묶음 후보를 만드는 화면입니다.</span>
    <span class="muted">status_update_allowed=false · db_writes=false · 사람 결정 없이 human_reviewed를 쓰지 않습니다.</span>
  </section>

  <section class="panel">
    <div class="toolbar">
      <div class="field"><label>기준 대분류</label><input id="seedMajorCode" class="code" value="01"></div>
      <div class="field"><label>대상 대분류</label><input id="majorCode" class="code" value=""></div>
      <div class="field"><label>중분류</label><input id="middleCode" class="code" value=""></div>
      <div class="field"><label>소분류</label><input id="smallCode" class="code" value=""></div>
      <div class="field"><label>세분류</label><input id="subCode" class="code" value=""></div>
      <div class="field"><label>샘플</label><input id="sampleLimit" class="code" value="5"></div>
      <button onclick="loadPatterns()">유형 분류 조회</button>
      <button class="secondary" onclick="setMajor01()">01만 보기</button>
      <button class="secondary" onclick="clearTarget()">전체 보기</button>
    </div>
    <div id="status" class="muted" style="margin-top:10px;"></div>
  </section>

  <section class="panel">
    <h2>요약</h2>
    <div id="summary" class="summary"></div>
  </section>

  <section class="panel">
    <h2>유형별 묶음</h2>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>유형</th>
            <th>위험도</th>
            <th>라벨 후보</th>
            <th>고유 라벨</th>
            <th>현재 상태</th>
            <th>판단 가이드</th>
          </tr>
        </thead>
        <tbody id="groups">
          <tr><td colspan="6" class="muted">조회 버튼을 누르세요.</td></tr>
        </tbody>
      </table>
    </div>
  </section>

  <section class="panel">
    <h2>샘플</h2>
    <div id="samples" class="samples"><span class="muted">유형 행을 클릭하면 샘플이 표시됩니다.</span></div>
  </section>
</main>
<script>
const q = id => document.getElementById(id);
const fmt = new Intl.NumberFormat('ko-KR');
let latestGroups = [];
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function num(value) {
  const n = Number(value || 0);
  return Number.isFinite(n) ? n : 0;
}
function params() {
  const p = new URLSearchParams();
  for (const [id, key] of [
    ['seedMajorCode', 'seed_major_code'],
    ['majorCode', 'major_code'],
    ['middleCode', 'middle_code'],
    ['smallCode', 'small_code'],
    ['subCode', 'sub_code'],
    ['sampleLimit', 'limit'],
  ]) {
    const value = q(id).value.trim();
    if (value) p.set(key, value);
  }
  return p;
}
function riskClass(risk) {
  if (risk === 'low' || risk === 'done') return 'good';
  if (risk === 'high') return 'bad';
  return 'warn';
}
function metric(label, value, detail='') {
  return `<div class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong><div class="muted">${esc(detail)}</div></div>`;
}
async function fetchJson(path) {
  const response = await fetch(path);
  const data = await response.json();
  if (!response.ok || data.ok === false) throw new Error((data.error && data.error.detail) || data.error || 'request failed');
  return data;
}
function renderSummary(data) {
  const summary = data.summary || {};
  const seed = data.seed_summary || {};
  q('summary').innerHTML = [
    metric('대상 라벨 후보', fmt.format(num(summary.target_label_count)), '현재 대상 범위'),
    metric('묶음 승인 후보', fmt.format(num(summary.batch_candidate_count)), '자동 승인 아님'),
    metric('먼저 볼 후보', fmt.format(num(summary.manual_review_first_count)), '보류/전문/미분류'),
    metric('기준 감사 라벨', fmt.format(num(seed.audited_human_label_count)), `대분류 ${seed.major_code || ''}`),
  ].join('');
}
function renderGroups(groups) {
  if (!groups.length) {
    q('groups').innerHTML = '<tr><td colspan="6" class="muted">유형 분류 결과가 없습니다.</td></tr>';
    return;
  }
  q('groups').innerHTML = groups.map((group, index) => `
    <tr class="group-row" onclick="renderSamples(${index})">
      <td><strong>${esc(group.label)}</strong><br><span class="muted">${esc(group.id)}</span></td>
      <td><span class="pill ${riskClass(group.risk)}">${esc(group.risk)}</span></td>
      <td>${fmt.format(num(group.label_count))}<br><span class="muted">${num(group.share_percent).toFixed(1)}%</span></td>
      <td>${fmt.format(num(group.distinct_label_count))}</td>
      <td>
        <span class="pill good">감사기록 ${fmt.format(num(group.existing_trusted_status_count ?? group.human_reviewed_count))}</span>
        <span class="pill warn">LLM ${fmt.format(num(group.llm_reviewed_count))}</span>
        <span class="pill bad">보류 ${fmt.format(num(group.needs_review_count))}</span>
      </td>
      <td>${esc(group.decision_hint || '')}</td>
    </tr>
  `).join('');
}
function renderSamples(index) {
  const group = latestGroups[index];
  if (!group) return;
  const samples = group.samples || [];
  q('samples').innerHTML = `
    <div><span class="pill ${riskClass(group.risk)}">${esc(group.label)}</span> <span class="muted">${esc(group.decision_hint || '')}</span></div>
    ${samples.length ? samples.map(sample => `
      <div class="sample">
        <strong>${esc(sample.label_text)}</strong>
        <div class="muted">원문: ${esc(sample.source_text)}</div>
        <div><code>${esc(sample.major_code)} ${esc(sample.major_name)} / ${esc(sample.unit_code)} ${esc(sample.unit_name_raw)}</code></div>
        <div>
          <span class="pill">${esc(sample.review_status)}</span>
          <span class="pill">${esc(sample.source_method)}</span>
          <span class="pill">${esc(sample.concept_type)}</span>
        </div>
      </div>
    `).join('') : '<span class="muted">샘플 없음</span>'}
  `;
}
async function loadPatterns() {
  q('status').textContent = '유형 분류를 계산하는 중입니다...';
  const data = await fetchJson('/api/ksa-label-patterns?' + params().toString());
  latestGroups = data.groups || [];
  renderSummary(data);
  renderGroups(latestGroups);
  q('samples').innerHTML = '<span class="muted">유형 행을 클릭하면 샘플이 표시됩니다.</span>';
  q('status').textContent = `${fmt.format(latestGroups.length)}개 유형을 불러왔습니다.`;
}
function setMajor01() {
  q('majorCode').value = '01';
  q('middleCode').value = '';
  q('smallCode').value = '';
  q('subCode').value = '';
  loadPatterns();
}
function clearTarget() {
  q('majorCode').value = '';
  q('middleCode').value = '';
  q('smallCode').value = '';
  q('subCode').value = '';
  loadPatterns();
}
loadPatterns().catch(err => { q('status').textContent = err.message; });
</script>
</body>
</html>
"""


def render_ksa_label_auto_triage_html() -> str:
    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KSA Label Auto Triage</title>
  <style>
    :root { --accent:#1d4ed8; --line:#d7dde8; --muted:#64748b; --bg:#f7f9fc; --good:#15803d; --warn:#b45309; --bad:#b91c1c; --violet:#6d28d9; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--bg); color:#111827; }
    main { max-width:1500px; margin:0 auto; padding:20px; display:grid; gap:14px; }
    h1, h2 { margin:0; }
    .muted { color:var(--muted); }
    .topbar { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }
    .panel { background:#fff; border:1px solid var(--line); border-radius:8px; padding:14px; box-shadow:0 1px 2px rgba(15,23,42,.05); }
    .notice { border-left:4px solid var(--accent); background:#eff6ff; display:grid; gap:4px; }
    .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:flex-end; }
    .field { display:grid; gap:4px; }
    label { font-size:12px; font-weight:700; color:#475569; }
    input, select { height:36px; border:1px solid #cbd5e1; border-radius:6px; padding:0 9px; font-size:14px; background:#fff; }
    input.code { width:88px; }
    button, a.button-link { min-height:36px; border:1px solid var(--accent); border-radius:6px; background:var(--accent); color:#fff; padding:8px 12px; font-weight:700; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; gap:6px; }
    button.secondary, a.button-link.secondary { background:#fff; color:var(--accent); }
    .summary { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:10px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:10px; background:#fff; display:grid; gap:4px; min-height:78px; }
    .metric span { color:var(--muted); font-size:12px; }
    .metric strong { font-size:22px; }
    .scroll { overflow:auto; border:1px solid var(--line); border-radius:8px; }
    pre { margin:8px 0 0; overflow:auto; border:1px solid var(--line); border-radius:8px; background:#f8fafc; padding:10px; font-size:12px; line-height:1.45; }
    table { width:100%; border-collapse:collapse; min-width:1180px; }
    th, td { border-bottom:1px solid #e5e7eb; padding:9px 10px; text-align:left; vertical-align:top; }
    th { background:#f1f5f9; font-size:12px; color:#334155; }
    .pill { display:inline-flex; align-items:center; border-radius:999px; padding:3px 8px; font-size:12px; font-weight:700; background:#e2e8f0; color:#334155; margin:1px; white-space:nowrap; }
    .auto_pass_candidate { background:#dcfce7; color:#166534; }
    .revise_recommended { background:#fef3c7; color:#92400e; }
    .human_sample_required { background:#ede9fe; color:#5b21b6; }
    .domain_expert_required { background:#fee2e2; color:#991b1b; }
    .missing_label_gap { background:#e2e8f0; color:#334155; }
    .auto-pass-candidate { background:#dcfce7; color:#166534; }
    .modify-recommended { background:#fef3c7; color:#92400e; }
    .human-sample-needed { background:#ede9fe; color:#5b21b6; }
    .domain-expert-needed { background:#fee2e2; color:#991b1b; }
    .already-trusted-review, .missing-label-gap { background:#e2e8f0; color:#334155; }
    code { color:#475569; white-space:normal; }
    @media (max-width:950px) { main { padding:12px; } .topbar { display:grid; } .summary { grid-template-columns:1fr; } .field, button, a.button-link { width:100%; } input.code { width:100%; } }
  </style>
</head>
<body>
<main>
  <header class="topbar">
    <div>
      <h1>KSA Label Auto Triage</h1>
      <p class="muted">Read-only recommendation buckets from HR-reviewed sample labels. This page does not approve or write review status.</p>
    </div>
    <div style="display:flex; gap:8px; flex-wrap:wrap;">
      <a class="button-link secondary" href="/ksa-label-patterns">Pattern Classifier</a>
      <a class="button-link secondary" href="/ksa-review-dashboard">Label Review</a>
      <a class="button-link secondary" href="/">Main Dashboard</a>
    </div>
  </header>

  <section class="panel notice">
    <strong>Read-only policy</strong>
    <span>auto_pass_candidate means display/review-queue minimization only. It is not human approval.</span>
    <span>Scoped counts are a local view. Use the all-scope policy-v2 report and operator sampling plan for bulk planning.</span>
    <span class="muted">status_update_allowed=false / db_writes=false / approval_claim=false</span>
  </section>

  <section class="panel">
    <h2>Operator Path</h2>
    <p class="muted">This page is the triage surface. For canonical bulk planning, export an all-scope report, build the policy-v2 sampling plan, then use the handoff index. The generated CSV keeps human decision fields blank.</p>
    <pre><code>python scripts\\ncs_harness.py ksa-label-auto-triage-report --trusted-major-code 02 --trusted-middle-code 02 --trusted-small-code 02 --out reports\\ksa_label_auto_triage_all.json --markdown-out reports\\ksa_label_auto_triage_all.md --csv-out reports\\ksa_label_auto_triage_all.csv
python scripts\\ncs_harness.py ksa-label-policy-v2-sampling-plan --source-report reports\\ksa_label_auto_triage_all.json --out reports\\ksa_label_policy_v2_sampling_plan.json --markdown-out reports\\ksa_label_policy_v2_sampling_plan.md --csv-out reports\\ksa_label_policy_v2_sampling_plan.csv</code></pre>
    <p class="muted">Latest overnight handoff artifacts follow `ksa_label_policy_v2_operator_sampling_plan*.csv` and `ksa_label_policy_v2_operator_handoff_index*.json` under `reports\\overnight_sessions\\readonly_refresh`.</p>
  </section>

  <section class="panel">
    <div class="toolbar">
      <div class="field"><label>major</label><input id="majorCode" class="code" value=""></div>
      <div class="field"><label>middle</label><input id="middleCode" class="code" value=""></div>
      <div class="field"><label>small</label><input id="smallCode" class="code" value=""></div>
      <div class="field"><label>sub</label><input id="subCode" class="code" value=""></div>
      <div class="field"><label>trusted major</label><input id="trustedMajorCode" class="code" value="02"></div>
      <div class="field"><label>trusted middle</label><input id="trustedMiddleCode" class="code" value="02"></div>
      <div class="field"><label>trusted small</label><input id="trustedSmallCode" class="code" value="02"></div>
      <div class="field"><label>limit</label><input id="limit" class="code" value="200"></div>
      <div class="field"><label>samples</label><input id="sampleLimit" class="code" value="5"></div>
      <div class="field">
        <label>bucket</label>
        <select id="bucketFilter">
          <option value="">all</option>
          <option value="auto_pass_candidate">auto pass</option>
          <option value="revise_recommended">revise</option>
          <option value="human_sample_required">human sample</option>
          <option value="domain_expert_required">domain expert</option>
          <option value="missing_label_gap">missing gap</option>
        </select>
      </div>
      <div class="field">
        <label>classification_v2</label>
        <select id="classificationFilter">
          <option value="">all</option>
          <option value="auto-pass-candidate">auto-pass-candidate</option>
          <option value="modify-recommended">modify-recommended</option>
          <option value="human-sample-needed">human-sample-needed</option>
          <option value="domain-expert-needed">domain-expert-needed</option>
          <option value="already-trusted-review">already-trusted-review</option>
          <option value="missing-label-gap">missing-label-gap</option>
        </select>
      </div>
      <button onclick="loadAutoTriage()">조회</button>
      <button class="secondary" onclick="setHrScope()">HR 02-02-02</button>
      <button class="secondary" onclick="clearScope()">전체</button>
    </div>
    <div id="status" class="muted" style="margin-top:10px;"></div>
  </section>

  <section class="panel">
    <h2>Summary</h2>
    <div id="summary" class="summary"></div>
  </section>

  <section class="panel">
    <h2>Rows</h2>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>bucket</th>
            <th>label</th>
            <th>source</th>
            <th>rule</th>
            <th>status/method</th>
            <th>flags</th>
            <th>scope</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </section>
</main>
<script>
const q = id => document.getElementById(id);
const fmt = new Intl.NumberFormat('ko-KR');
let latestRows = [];
function normalizeStaticLabels() {
  for (const [selector, text] of [
    ['button[onclick="loadAutoTriage()"]', 'Load'],
    ['button[onclick="clearScope()"]', 'All'],
  ]) {
    const node = document.querySelector(selector);
    if (node) node.textContent = text;
  }
}
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function params() {
  const p = new URLSearchParams();
  for (const [key, id] of [
    ['major_code','majorCode'],
    ['middle_code','middleCode'],
    ['small_code','smallCode'],
    ['sub_code','subCode'],
    ['trusted_major_code','trustedMajorCode'],
    ['trusted_middle_code','trustedMiddleCode'],
    ['trusted_small_code','trustedSmallCode'],
    ['limit','limit'],
    ['sample_limit','sampleLimit'],
  ]) {
    const value = (q(id).value || '').trim();
    if (value) p.set(key, value);
  }
  return p;
}
async function fetchJson(url) {
  const response = await fetch(url);
  const data = await response.json();
  if (!response.ok || data.ok === false) throw new Error(errorMessage(data.error, response.statusText));
  return data;
}
function errorMessage(error, fallback='request failed') {
  if (!error) return fallback;
  if (typeof error === 'string') return error;
  if (typeof error.detail === 'string') return `${error.code ? error.code + ': ' : ''}${error.detail}`;
  if (error.code) return error.code;
  return fallback;
}
function metric(label, value, note='') {
  return `<div class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong>${note ? `<span>${esc(note)}</span>` : ''}</div>`;
}
function renderSummary(data) {
  const buckets = data.classification_bucket_counts || data.bucket_counts || {};
  const classificationV2 = data.classification_v2_counts || {};
  const counts = data.counts || {};
  const decision = data.decision_summary || {};
  const fullDecisionRows = decision.full_scope_decision_row_count ?? data.full_scope_decision_row_count ?? counts.full_scope_decision_rows ?? 0;
  const manualReviewRows = decision.full_scope_manual_review_recommended_count ?? data.full_scope_manual_review_recommended_count ?? counts.full_scope_manual_review_recommended_rows ?? 0;
  const emittedDecisionRows = decision.emitted_decision_row_count ?? data.emitted_decision_row_count ?? data.decision_row_count ?? counts.emitted_decision_rows ?? counts.decision_sheet_rows ?? 0;
  const orphanRawBacklog = decision.full_scope_orphan_raw_concept_backlog_count ?? data.full_scope_orphan_raw_concept_backlog_count ?? counts.full_scope_orphan_raw_concept_backlog ?? 0;
  const outputLimited = decision.output_limit_applied ?? data.output_limit_applied ?? false;
  const scopePolicy = data.scope_policy || {};
  const scopeNote = scopePolicy.target_scope_is_filtered ? 'scoped local view' : 'all-scope view';
  q('summary').innerHTML = [
    metric('scope policy', scopeNote, 'all-scope required for bulk'),
    metric('label candidates', fmt.format(data.candidate_count ?? counts.label_candidates ?? 0)),
    metric('operator decisions', fmt.format(fullDecisionRows), 'full scope, excludes already trusted/gaps'),
    metric('manual review rec.', fmt.format(manualReviewRows), 'excludes auto-pass candidates'),
    metric('exported decisions', fmt.format(emittedDecisionRows), outputLimited ? 'limited output' : 'all emitted'),
    metric('trusted sample', fmt.format(data.trusted_sample_count ?? counts.trusted_sample_label_candidates ?? 0), '02 sample basis only'),
    metric('v2 auto-pass', fmt.format(classificationV2['auto-pass-candidate'] || 0)),
    metric('v2 modify', fmt.format(classificationV2['modify-recommended'] || 0)),
    metric('v2 human sample', fmt.format(classificationV2['human-sample-needed'] || 0)),
    metric('v2 domain expert', fmt.format(classificationV2['domain-expert-needed'] || 0)),
    metric('auto pass', fmt.format(buckets.auto_pass_candidate || 0)),
    metric('revise', fmt.format(buckets.revise_recommended || 0)),
    metric('human sample', fmt.format(buckets.human_sample_required || 0)),
    metric('domain expert', fmt.format(buckets.domain_expert_required || 0)),
    metric('missing gap', fmt.format(buckets.missing_label_gap || 0)),
    metric('orphan raw backlog', fmt.format(orphanRawBacklog), 'excluded from review rows'),
    metric('already trusted', fmt.format(decision.full_scope_already_trusted_reviewed_count ?? data.full_scope_already_trusted_reviewed_count ?? counts.full_scope_already_trusted_reviewed_rows ?? 0), 'audited existing rows'),
    metric('emitted rows', fmt.format(data.emitted_row_count ?? counts.emitted_rows ?? 0)),
    metric('db writes', String(data.db_writes)),
    metric('approval claim', String(data.approval_claim)),
  ].join('');
}
function renderRows(rows) {
  const bucketFilter = q('bucketFilter').value;
  const classificationFilter = q('classificationFilter').value;
  const visibleRows = rows.filter(row => {
    if (bucketFilter && (row.recommendation_bucket || '') !== bucketFilter) return false;
    if (classificationFilter && (row.classification_v2 || '') !== classificationFilter) return false;
    return true;
  });
  if (!visibleRows.length) {
    q('rows').innerHTML = '<tr><td colspan="7" class="muted">No rows.</td></tr>';
    const filterText = [bucketFilter, classificationFilter].filter(Boolean).join(' / ');
    q('status').textContent = `${fmt.format(rows.length)} rows loaded. ${filterText ? '0 visible for ' + filterText + '.' : ''}`;
    return;
  }
  q('rows').innerHTML = visibleRows.map(row => {
    const bucket = row.recommendation_bucket || 'unknown';
    const classificationV2 = row.classification_v2 || 'unknown';
    const flags = (row.quality_flags || []).map(flag => `<span class="pill">${esc(flag)}</span>`).join('');
    return `<tr>
      <td><span class="pill ${esc(bucket)}">${esc(bucket)}</span><br><span class="pill ${esc(classificationV2)}">${esc(classificationV2)}</span></td>
      <td><strong>${esc(row.label_text || '')}</strong><br><code>#${esc(row.label_id || '')} concept ${esc(row.concept_id || '')}</code></td>
      <td>${esc(row.source_text || '')}</td>
      <td><strong>${esc(row.recommendation_rule || '')}</strong><br><span class="muted">${esc(row.recommendation_rationale || '')}</span></td>
      <td><span class="pill">${esc(row.review_status || '')}</span><span class="pill">${esc(row.source_method || '')}</span><br><span class="muted">confidence ${esc(row.confidence_score ?? '')}</span></td>
      <td>${flags || '<span class="muted">none</span>'}</td>
      <td><code>${esc(row.source_scope_key || '')}</code><br><span class="muted">${esc(row.hr_sample_support || '')}</span></td>
    </tr>`;
  }).join('');
  q('status').textContent = `${fmt.format(rows.length)} rows loaded. ${fmt.format(visibleRows.length)} visible.`;
}
async function loadAutoTriage() {
  try {
  q('status').textContent = '계산 중...';
  q('status').textContent = 'Loading...';
  const data = await fetchJson('/api/ksa-label-auto-triage?' + params().toString());
  renderSummary(data);
  latestRows = data.rows || [];
  renderRows(latestRows);
  } catch (err) {
    latestRows = [];
    q('rows').innerHTML = '<tr><td colspan="7" class="muted">No rows.</td></tr>';
    q('status').textContent = err.message;
  }
}
function setHrScope() {
  q('majorCode').value = '02';
  q('middleCode').value = '02';
  q('smallCode').value = '02';
  q('subCode').value = '';
  loadAutoTriage();
}
function clearScope() {
  q('majorCode').value = '';
  q('middleCode').value = '';
  q('smallCode').value = '';
  q('subCode').value = '';
  loadAutoTriage();
}
normalizeStaticLabels();
q('bucketFilter').addEventListener('change', () => renderRows(latestRows));
q('classificationFilter').addEventListener('change', () => renderRows(latestRows));
loadAutoTriage();
</script>
</body>
</html>
"""


def render_ksa_review_dashboard_html() -> str:
    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KSA Review Dashboard</title>
  <style>
    :root {
      --bg:#f6f7fb; --panel:#ffffff; --line:#d9dee9; --text:#172033; --muted:#657184;
      --primary:#2457d6; --primary-soft:#eaf0ff; --good:#0f8a4b; --warn:#a96700; --bad:#b42318;
    }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font-family:Segoe UI, "Malgun Gothic", Arial, sans-serif; }
    main { max-width:1480px; margin:0 auto; padding:20px; }
    h1, h2 { margin:0; letter-spacing:0; }
    h1 { font-size:26px; }
    h2 { font-size:18px; }
    .muted { color:var(--muted); font-size:13px; line-height:1.45; }
    .topbar, .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; box-shadow:0 8px 18px rgba(18,28,45,.05); }
    .topbar { padding:18px 20px; margin-bottom:14px; display:flex; justify-content:space-between; gap:16px; align-items:center; }
    .panel { padding:16px; margin-bottom:14px; }
    .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:end; }
    .field { display:grid; gap:5px; }
    label { font-size:12px; font-weight:700; color:#3b4658; }
    input, select { height:36px; border:1px solid var(--line); border-radius:6px; padding:0 10px; background:#fff; color:var(--text); }
    input.code { width:72px; }
    input.keyword { min-width:260px; }
    button, a.button-link {
      height:36px; border:1px solid var(--primary); border-radius:6px; padding:0 12px; background:var(--primary);
      color:#fff; font-weight:700; cursor:pointer; display:inline-flex; align-items:center; gap:6px; text-decoration:none; white-space:nowrap;
    }
    button.secondary { background:#fff; color:var(--primary); }
    button.good { background:var(--good); border-color:var(--good); }
    button.bad { background:var(--bad); border-color:var(--bad); }
    button:disabled { opacity:.55; cursor:wait; }
    .button-icon { width:18px; height:18px; display:inline-grid; place-items:center; font-size:14px; line-height:1; }
    .status { margin-top:10px; min-height:22px; }
    .status.error { color:var(--bad); font-weight:700; }
    .notice { margin-top:10px; border:1px solid #c7d2fe; background:#f8faff; border-radius:8px; padding:10px 12px; display:grid; gap:5px; }
    .review-summary { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:10px; background:#fff; min-height:76px; }
    .metric span { display:block; color:var(--muted); font-size:12px; }
    .metric strong { display:block; margin-top:4px; font-size:22px; }
    .metric.good { border-color:#9bd7b5; background:#f2fbf6; }
    .metric.warn { border-color:#f2c781; background:#fff8eb; }
    .metric.bad { border-color:#f1aaa4; background:#fff4f2; }
    .scope-layout { display:block; }
    .major-grid { margin-top:10px; display:grid; grid-template-columns:repeat(8,minmax(0,1fr)); gap:8px; }
    .major-tile, .node {
      height:auto; min-height:54px; border:1px solid var(--line); background:#fff; color:var(--text);
      justify-content:flex-start; text-align:left; gap:8px; border-radius:8px; padding:9px;
    }
    .major-tile.active, .node.active { border-color:var(--primary); background:var(--primary-soft); color:var(--primary); }
    .major-icon, .node-icon { width:28px; height:28px; border-radius:6px; background:#eef2f7; color:#2e3a4e; display:grid; place-items:center; font-weight:800; flex:0 0 auto; }
    .major-tile { display:grid; grid-template-columns:34px minmax(0,1fr); align-items:center; min-height:70px; }
    .major-tile > span:last-child { min-width:0; display:grid; gap:2px; }
    .tile-title, .node-title { display:block; font-weight:800; line-height:1.3; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .tile-meta, .node-sub { display:block; color:var(--muted); font-size:12px; line-height:1.25; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .taxonomy-columns { margin-top:14px; display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }
    .taxonomy-column { border:1px solid var(--line); border-radius:8px; background:#fff; min-height:130px; }
    .taxonomy-column header { padding:10px 12px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; gap:8px; }
    .taxonomy-list { padding:8px; display:grid; gap:6px; max-height:270px; overflow:auto; }
    .node { width:100%; display:flex; }
    .node-main { display:grid; gap:2px; min-width:0; }
    .scope-progress { margin-top:12px; border:1px solid var(--line); border-radius:8px; padding:12px; display:grid; gap:8px; background:#fff; }
    .progress { height:10px; border-radius:999px; background:#e6eaf2; overflow:hidden; }
    .progress span { display:block; height:100%; background:var(--primary); }
    .scroll { overflow:auto; border:1px solid var(--line); border-radius:8px; }
    table { width:100%; border-collapse:collapse; min-width:1120px; background:#fff; }
    th, td { padding:11px 12px; border-bottom:1px solid var(--line); vertical-align:top; text-align:left; }
    th { position:sticky; top:0; background:#f1f4f9; z-index:1; font-size:13px; color:#3d4658; }
    .label-candidate { display:grid; gap:7px; }
    .label-candidate strong { font-size:18px; line-height:1.35; }
    .raw { font-size:14px; line-height:1.5; white-space:pre-wrap; }
    .pill { display:inline-flex; width:max-content; align-items:center; border-radius:999px; padding:3px 8px; font-size:12px; font-weight:800; background:#eef2f7; color:#3a475a; }
    .pill.good { background:#e6f7ee; color:var(--good); }
    .pill.warn { background:#fff1d6; color:var(--warn); }
    .pill.bad { background:#ffe7e4; color:var(--bad); }
    .review-actions { display:grid; gap:7px; min-width:150px; }
    .review-actions button { width:100%; justify-content:center; }
    .label-edit-box { display:grid; gap:6px; margin-top:4px; }
    .label-edit-box input { width:100%; height:34px; font-size:13px; }
    .row-status { margin-top:7px; font-size:12px; line-height:1.35; color:var(--muted); }
    .row-status.error { color:var(--bad); font-weight:700; }
    .row-status.good { color:var(--good); font-weight:700; }
    @media (max-width:1120px) {
      .review-summary { grid-template-columns:repeat(3,minmax(0,1fr)); }
      .taxonomy-columns { grid-template-columns:1fr; }
      .major-grid { grid-template-columns:repeat(4,minmax(0,1fr)); }
    }
    @media (max-width:680px) {
      main { padding:12px; }
      .topbar { display:grid; }
      .review-summary { grid-template-columns:1fr; }
      .major-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
      input.keyword, .field, button, a.button-link { width:100%; }
    }
  </style>
</head>
<body>
<main>
  <header class="topbar">
    <div>
      <h1>KSA Review Dashboard</h1>
      <p class="muted">단어형 KSA 라벨 후보만 검토하는 전용 화면입니다.</p>
    </div>
    <a class="button-link" href="/ksa-definition-dashboard">정의 대시보드</a>
  </header>

  <section class="panel">
    <div class="toolbar">
      <div class="field"><label>대분류</label><input id="majorCode" class="code" value=""></div>
      <div class="field"><label>중분류</label><input id="middleCode" class="code" value=""></div>
      <div class="field"><label>소분류</label><input id="smallCode" class="code" value=""></div>
      <div class="field"><label>세분류</label><input id="subCode" class="code" value=""></div>
      <div class="field"><label>검색어</label><input id="keyword" class="keyword" placeholder="KSA, 능력단위, 라벨명"></div>
      <div class="field">
        <label>라벨 리뷰</label>
        <select id="labelReviewStatus">
          <option value="all">전체</option>
          <option value="llm_reviewed" selected>LLM 단어형 검토됨</option>
          <option value="candidate">라벨 전처리 후보</option>
          <option value="needs_review">수정필요</option>
          <option value="human_reviewed">사람확인</option>
          <option value="rejected">거절</option>
          <option value="missing">후보 없음</option>
        </select>
      </div>
      <div class="field"><label>표시</label><input id="limit" class="code" value="50"></div>
      <div class="field"><label>Review note</label><input id="reviewNote" class="keyword" placeholder="human review note"></div>
      <div class="field"><label>Decision packet</label><input id="reviewSourcePacket" class="keyword" placeholder="reports/...csv#label:123:approve"></div>
      <div class="field"><label>Packet SHA-256</label><input id="reviewSourceHash" class="keyword" placeholder="sha256:..."></div>
      <input id="reviewerId" type="hidden" value="__DASHBOARD_DEFAULT_REVIEWER__">
      <button onclick="refreshReviewDashboard()"><span class="button-icon" aria-hidden="true">🔎</span>조회</button>
      <button class="secondary" onclick="setHrScope()"><span class="button-icon" aria-hidden="true">🏢</span>인사 직무</button>
      <button class="secondary" onclick="setLabelPendingOnly()"><span class="button-icon" aria-hidden="true">☑</span>LLM 단어형</button>
      <button class="secondary" onclick="setNeedsReviewOnly()"><span class="button-icon" aria-hidden="true">✎</span>수정대상</button>
      <button class="secondary" onclick="clearFilters()"><span class="button-icon" aria-hidden="true">↺</span>전체</button>
    </div>
    <div id="status" class="status muted"></div>
    <div id="labelCandidateNotice" class="notice">
      <strong>단어형 KSA 라벨 후보를 불러오는 중입니다.</strong>
      <span class="muted">원문 KSA는 그대로 두고, 별도 라벨 후보만 검토합니다.</span>
    </div>
  </section>

  <section class="panel">
    <h2>리뷰 현황</h2>
    <div id="reviewSummary" class="review-summary"></div>
  </section>

  <section class="panel">
    <div class="scope-layout">
      <div>
        <h2>분류 선택</h2>
        <div class="muted">대분류부터 세분류까지 클릭하면 해당 범위의 단어형 KSA 리뷰 진행률과 목록을 다시 조회합니다.</div>
        <div id="majorTiles" class="major-grid"></div>
      </div>
      <div id="scopeProgress" class="scope-progress">
        <span class="muted">리뷰 범위를 선택하세요.</span>
      </div>
    </div>
    <div class="taxonomy-columns">
      <section class="taxonomy-column">
        <header><strong>중분류</strong><span id="middleMeta" class="muted"></span></header>
        <div id="middleList" class="taxonomy-list"><span class="muted">대분류를 선택하세요.</span></div>
      </section>
      <section class="taxonomy-column">
        <header><strong>소분류</strong><span id="smallMeta" class="muted"></span></header>
        <div id="smallList" class="taxonomy-list"><span class="muted">중분류를 선택하세요.</span></div>
      </section>
      <section class="taxonomy-column">
        <header><strong>세분류</strong><span id="subMeta" class="muted"></span></header>
        <div id="subList" class="taxonomy-list"><span class="muted">소분류를 선택하세요.</span></div>
      </section>
    </div>
  </section>

  <section class="panel">
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th style="width:320px;">단어형 KSA</th>
            <th style="width:620px;">원문 KSA</th>
            <th style="width:220px;">현재 상태</th>
            <th style="width:260px;">사람확인 버튼</th>
          </tr>
        </thead>
        <tbody id="reviewItems">
          <tr><td colspan="4" class="muted">조회 버튼을 눌러 리뷰 항목을 불러오세요.</td></tr>
        </tbody>
      </table>
    </div>
  </section>
</main>
<script>
const q = id => document.getElementById(id);
const fmt = new Intl.NumberFormat('ko-KR');
const majorIcons = {
  '01':'📊','02':'🏢','03':'🏦','04':'🎓','05':'⚖️','06':'🏥',
  '07':'🤝','08':'🎭','09':'🚚','10':'💼','11':'🧹','12':'🏨',
  '13':'🍽️','14':'🏗️','15':'⚙️','16':'🧱','17':'🧪','18':'🧵',
  '19':'⚡','20':'💻','21':'🥫','22':'🛋️','23':'🌱','24':'🚜'
};
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function toNum(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? number : 0;
}
function progressBar(percent) {
  const value = Math.max(0, Math.min(100, toNum(percent)));
  return `<div class="progress"><span style="width:${value.toFixed(1)}%"></span></div>`;
}
function params() {
  const p = new URLSearchParams();
  for (const [id, key] of [
    ['majorCode', 'major_code'],
    ['middleCode', 'middle_code'],
    ['smallCode', 'small_code'],
    ['subCode', 'sub_code'],
    ['keyword', 'keyword'],
    ['labelReviewStatus', 'label_review_status'],
    ['limit', 'limit'],
  ]) {
    const value = q(id).value.trim();
    if (value && value !== 'all') p.set(key, value);
  }
  return p;
}
function selectedCodes() {
  return {
    major: q('majorCode').value.trim(),
    middle: q('middleCode').value.trim(),
    small: q('smallCode').value.trim(),
    sub: q('subCode').value.trim(),
  };
}
function setScope(major='', middle='', small='', sub='') {
  q('majorCode').value = major || '';
  q('middleCode').value = middle || '';
  q('smallCode').value = small || '';
  q('subCode').value = sub || '';
}
function taxonomyParams(level) {
  const p = new URLSearchParams();
  const codes = selectedCodes();
  p.set('level', level);
  p.set('limit', level === 'major' ? '100' : '500');
  if (codes.major) p.set('major_code', codes.major);
  if (codes.middle) p.set('middle_code', codes.middle);
  if (codes.small) p.set('small_code', codes.small);
  return p;
}
async function fetchJson(path, options={}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    const error = typeof data.error === 'string' ? data.error : (data.error?.detail || 'request failed');
    throw new Error(error);
  }
  return data;
}
function nodeName(target) {
  const el = q(target)?.querySelector('.active .node-title, .active .tile-title');
  return el ? el.textContent : '';
}
function currentScopeLabel() {
  const names = [nodeName('majorTiles'), nodeName('middleList'), nodeName('smallList'), nodeName('subList')].filter(Boolean);
  const codes = selectedCodes();
  const codeText = [codes.major, codes.middle, codes.small, codes.sub].filter(Boolean).join('-') || '전체 NCS';
  return names.length ? `${names.join(' > ')} (${codeText})` : codeText;
}
function renderMajorTiles(nodes) {
  const codes = selectedCodes();
  q('majorTiles').innerHTML = (nodes || []).map(node => {
    const active = node.major_code === codes.major;
    return `<button type="button" class="major-tile${active ? ' active' : ''}" onclick="selectMajor('${esc(node.major_code)}')">
      <span class="major-icon" aria-hidden="true">${majorIcons[node.major_code] || esc(node.major_code)}</span>
      <span>
        <span class="tile-title">${esc(node.major_code)}. ${esc(node.name || '')}</span>
        <span class="tile-meta">리뷰 범위 선택</span>
      </span>
    </button>`;
  }).join('');
}
function renderNodeList(target, metaTarget, nodes, level) {
  const codes = selectedCodes();
  q(metaTarget).textContent = nodes && nodes.length ? `${fmt.format(nodes.length)}개` : '';
  if (!nodes || !nodes.length) {
    const messages = {middle:'대분류를 선택하세요.', small:'중분류를 선택하세요.', sub:'소분류를 선택하세요.'};
    q(target).innerHTML = `<span class="muted">${esc(messages[level] || '결과가 없습니다.')}</span>`;
    return;
  }
  q(target).innerHTML = nodes.map(node => {
    const active =
      (level === 'middle' && node.middle_code === codes.middle) ||
      (level === 'small' && node.small_code === codes.small) ||
      (level === 'sub' && node.sub_code === codes.sub);
    const click =
      level === 'middle'
        ? `selectMiddle('${esc(node.major_code)}','${esc(node.middle_code)}')`
        : level === 'small'
          ? `selectSmall('${esc(node.major_code)}','${esc(node.middle_code)}','${esc(node.small_code)}')`
          : `selectSub('${esc(node.major_code)}','${esc(node.middle_code)}','${esc(node.small_code)}','${esc(node.sub_code)}')`;
    return `<button type="button" class="node${active ? ' active' : ''}" onclick="${click}">
      <span class="node-icon">${esc(node.code || '')}</span>
      <span class="node-main">
        <span class="node-title">${esc(node.name || '')}</span>
        <span class="node-sub">리뷰 범위 선택</span>
      </span>
    </button>`;
  }).join('');
}
async function loadTaxonomy() {
  const codes = selectedCodes();
  const majors = await fetchJson('/api/taxonomy?' + taxonomyParams('major').toString());
  const middles = codes.major ? await fetchJson('/api/taxonomy?' + taxonomyParams('middle').toString()) : {nodes:[]};
  const smalls = codes.major && codes.middle ? await fetchJson('/api/taxonomy?' + taxonomyParams('small').toString()) : {nodes:[]};
  const subs = codes.major && codes.middle && codes.small ? await fetchJson('/api/taxonomy?' + taxonomyParams('sub').toString()) : {nodes:[]};
  renderMajorTiles(majors.nodes || []);
  renderNodeList('middleList', 'middleMeta', middles.nodes || [], 'middle');
  renderNodeList('smallList', 'smallMeta', smalls.nodes || [], 'small');
  renderNodeList('subList', 'subMeta', subs.nodes || [], 'sub');
}
async function selectMajor(major) { setScope(major, '', '', ''); await refreshReviewDashboard(); }
async function selectMiddle(major, middle) { setScope(major, middle, '', ''); await refreshReviewDashboard(); }
async function selectSmall(major, middle, small) { setScope(major, middle, small, ''); await refreshReviewDashboard(); }
async function selectSub(major, middle, small, sub) { setScope(major, middle, small, sub); await refreshReviewDashboard(); }
function metric(label, value, detail, cls='') {
  return `<div class="metric ${esc(cls)}"><span>${esc(label)}</span><strong>${esc(value)}</strong><div class="muted">${esc(detail || '')}</div></div>`;
}
function renderSummary(data) {
  const lp = data.label_review_scope_progress || data.label_review_progress || {};
  const total = toNum(lp.total);
  const human = toNum(lp.human_reviewed);
  const pending = toNum(lp.pending);
  const llmReviewed = toNum(lp.llm_reviewed);
  const needs = toNum(lp.needs_review);
  const rejected = toNum(lp.rejected);
  const humanPercent = toNum(lp.human_reviewed_percent);
  const reviewQueue = llmReviewed + pending + needs;
  const visibleRows = toNum((data.items || []).length);
  q('reviewSummary').innerHTML = [
    metric('라벨 리뷰율', `${humanPercent.toFixed(1)}%`, `${fmt.format(human)} / ${fmt.format(total)}`, human ? 'good' : 'warn'),
    metric('LLM 단어형', fmt.format(llmReviewed), '자동 점검 통과 후보', llmReviewed ? 'warn' : 'good'),
    metric('라벨 검토대기', fmt.format(reviewQueue), `후보 ${fmt.format(pending)} / 수정대상 ${fmt.format(needs)}`, reviewQueue ? 'warn' : 'good'),
    metric('수정대상', fmt.format(needs), '수정저장 필요', needs ? 'warn' : 'good'),
    metric('거절', fmt.format(rejected), '제외 후보', rejected ? 'warn' : 'good'),
    metric('현재 표시', fmt.format(visibleRows), '화면 행 수', 'good'),
  ].join('');
  q('scopeProgress').innerHTML = `
    <span class="muted">현재 리뷰 범위</span>
    <strong>${esc(currentScopeLabel())}</strong>
    <div><b>라벨 사람확인</b> ${fmt.format(human)} / ${fmt.format(total)} (${humanPercent.toFixed(1)}%)</div>
    ${progressBar(humanPercent)}
    <div><b>검토대기</b> ${fmt.format(reviewQueue)}건 · 수정대상 ${fmt.format(needs)}건 · 화면 표시 ${fmt.format(visibleRows)}건</div>
  `;
}
function renderLabelCandidateNotice(data, visibleCount) {
  const lp = data.label_review_scope_progress || data.label_review_progress || {};
  const total = toNum(lp.total);
  const llmReviewed = toNum(lp.llm_reviewed);
  const pending = toNum(lp.pending);
  const needs = toNum(lp.needs_review);
  const human = toNum(lp.human_reviewed);
  const rejected = toNum(lp.rejected);
  const filter = q('labelReviewStatus').value || 'all';
  const filterLabel = {
    all: '전체',
    llm_reviewed: 'LLM 단어형 검토됨',
    candidate: '라벨 전처리 후보',
    needs_review: '수정대상',
    human_reviewed: '사람확인',
    rejected: '거절',
    missing: '후보 없음',
  }[filter] || filter;
  q('labelCandidateNotice').innerHTML = `
    <strong>단어형 KSA 라벨 ${fmt.format(total)}건 · 현재 화면 ${fmt.format(visibleCount)}건</strong>
    <span>필터: <b>${esc(filterLabel)}</b> · 범위 전체 기준 LLM 단어형 ${fmt.format(llmReviewed)} / 후보 ${fmt.format(pending)} / 수정대상 ${fmt.format(needs)} / 사람확인 ${fmt.format(human)} / 거절 ${fmt.format(rejected)}</span>
  `;
}
function labelStatusText(status) {
  return {
    llm_reviewed: 'LLM 단어형 검토됨',
    candidate: '라벨 전처리 후보',
    needs_review: '수정대상',
    human_reviewed: '사람확인',
    rejected: '거절',
    missing: '후보 없음',
  }[status] || status || '후보 없음';
}
function labelStatusClass(status) {
  if (status === 'human_reviewed') return 'good';
  if (status === 'needs_review' || status === 'rejected') return 'bad';
  return 'warn';
}
function renderRows(items) {
  if (!items.length) {
    q('reviewItems').innerHTML = `<tr><td colspan="4" class="muted">현재 조건에 맞는 단어형 KSA 라벨 후보가 없습니다. 분류나 라벨 리뷰 필터를 바꿔보세요.</td></tr>`;
    return;
  }
  q('reviewItems').innerHTML = items.map(item => {
    const labelStatus = item.short_label_review_status || 'missing';
    const statusLabel = labelStatusText(labelStatus);
    const statusClass = labelStatusClass(labelStatus);
    const label = item.short_label_candidate || '';
    const labelBlock = label
      ? `<div class="label-candidate"><span class="pill good">단어형 KSA</span><strong>${esc(label)}</strong></div>`
      : `<span class="muted">단어형 라벨 후보 없음</span>`;
    const rawMeta = [item.major_code, item.major_name, item.unit_name_raw || item.competency_unit_name].filter(Boolean).join(' / ');
    const labelButtons = item.short_label_id
      ? `<div class="review-actions">
          <button type="button" class="good" data-label-id="${Number(item.short_label_id)}" data-decision="approve"><span class="button-icon">✓</span>사람확인</button>
          <button type="button" class="bad" data-label-id="${Number(item.short_label_id)}" data-decision="reject"><span class="button-icon">×</span>거절</button>
          <div class="label-edit-box">
            <input id="labelEdit-${Number(item.short_label_id)}" value="${esc(label)}" maxlength="60" aria-label="수정 라벨">
            <button type="button" class="secondary" data-edit-label-id="${Number(item.short_label_id)}"><span class="button-icon">✎</span>수정저장</button>
          </div>
          <div class="row-status" id="rowStatus-${Number(item.short_label_id)}"></div>
        </div>`
      : '<span class="muted">라벨 후보 없음</span>';
    return `<tr>
      <td>${labelBlock}</td>
      <td><div class="raw">${esc(item.ksa_text_raw || '')}</div><div class="muted">${esc(rawMeta)}</div></td>
      <td><span class="pill ${esc(statusClass)}">${esc(statusLabel)}</span></td>
      <td>${labelButtons}</td>
    </tr>`;
  }).join('');
}
async function loadReviewItems() {
  const data = await fetchJson('/api/ksa-definitions?' + params().toString());
  renderSummary(data);
  renderLabelCandidateNotice(data, (data.items || []).length);
  renderRows(data.items || []);
  q('status').className = 'status muted';
  q('status').textContent = `${fmt.format((data.items || []).length)}개 라벨 후보를 불러왔습니다.`;
}
async function oneClickPayload(kind, targetId, decision) {
  const artifact = '/ksa-review-dashboard?' + params().toString();
  const packet = q('reviewSourcePacket').value.trim();
  const packetHash = q('reviewSourceHash').value.trim();
  const note = q('reviewNote').value.trim();
  if (!packet || !packetHash || !note) {
    throw new Error('사람확인/수정에는 review note, reports decision packet, packet SHA-256이 필요합니다.');
  }
  return {
    decision,
    reviewer_id: q('reviewerId').value || '',
    source_decision_packet: packet,
    source_artifact_hash: packetHash,
    run_artifact: artifact,
    notes: note,
    rationale: note,
  };
}
async function reviewShortLabel(labelId, decision) {
  const rowStatus = q(`rowStatus-${labelId}`);
  const buttons = Array.from(document.querySelectorAll(`button[data-label-id="${labelId}"]`));
  q('status').className = 'status muted';
  q('status').textContent = '저장 중...';
  if (rowStatus) {
    rowStatus.className = 'row-status';
    rowStatus.textContent = '저장 중...';
  }
  buttons.forEach(button => button.disabled = true);
  try {
    const payload = await oneClickPayload('label', labelId, decision);
    payload.label_id = labelId;
    payload.raw_to_label_checked = true;
    const data = await fetchJson('/api/ksa-label-review', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    const resultText = `저장됨: 라벨 ${data.label_id} ${decision === 'approve' ? '사람확인' : (decision === 'needs_revision' ? '수정필요' : '거절')}`;
    q('status').textContent = resultText;
    if (rowStatus) {
      rowStatus.className = 'row-status good';
      rowStatus.textContent = resultText;
    }
    if (decision === 'needs_revision') q('labelReviewStatus').value = 'needs_review';
    await loadReviewItems();
  } catch (err) {
    buttons.forEach(button => button.disabled = false);
    q('status').className = 'status error';
    q('status').textContent = err.message;
    if (rowStatus) {
      rowStatus.className = 'row-status error';
      rowStatus.textContent = err.message;
    }
    window.alert(`저장 실패: ${err.message}`);
  }
}
async function editShortLabel(labelId) {
  const input = q(`labelEdit-${labelId}`);
  const rowStatus = q(`rowStatus-${labelId}`);
  const buttons = Array.from(document.querySelectorAll(`button[data-label-id="${labelId}"], button[data-edit-label-id="${labelId}"]`));
  const corrected = (input?.value || '').trim();
  if (!corrected) {
    window.alert('수정할 단어형 KSA 라벨을 입력하세요.');
    return;
  }
  q('status').className = 'status muted';
  q('status').textContent = '수정 저장 중...';
  if (rowStatus) {
    rowStatus.className = 'row-status';
    rowStatus.textContent = '수정 저장 중...';
  }
  buttons.forEach(button => button.disabled = true);
  try {
    const payload = await oneClickPayload('label_edit', labelId, 'edit_approve');
    payload.label_id = labelId;
    payload.corrected_label_text = corrected;
    payload.raw_to_label_checked = true;
    const data = await fetchJson('/api/ksa-label-edit', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    const resultText = `수정저장됨: ${data.corrected_label_text}`;
    q('status').textContent = resultText;
    if (rowStatus) {
      rowStatus.className = 'row-status good';
      rowStatus.textContent = resultText;
    }
    await loadReviewItems();
  } catch (err) {
    buttons.forEach(button => button.disabled = false);
    q('status').className = 'status error';
    q('status').textContent = err.message;
    if (rowStatus) {
      rowStatus.className = 'row-status error';
      rowStatus.textContent = err.message;
    }
    window.alert(`수정 저장 실패: ${err.message}`);
  }
}
document.addEventListener('click', event => {
  const button = event.target.closest('button[data-label-id][data-decision]');
  const editButton = event.target.closest('button[data-edit-label-id]');
  if (button) {
    event.preventDefault();
    reviewShortLabel(Number(button.dataset.labelId), button.dataset.decision);
    return;
  }
  if (editButton) {
    event.preventDefault();
    editShortLabel(Number(editButton.dataset.editLabelId));
  }
});
async function refreshReviewDashboard() {
  q('status').className = 'status muted';
  q('status').textContent = 'Loading...';
  try {
    await loadTaxonomy();
    await loadReviewItems();
  } catch (err) {
    q('status').className = 'status error';
    q('status').textContent = err.message;
  }
}
async function setHrScope() {
  setScope('02', '02', '02', '01');
  q('labelReviewStatus').value = 'llm_reviewed';
  await refreshReviewDashboard();
}
async function setLabelPendingOnly() {
  q('labelReviewStatus').value = 'llm_reviewed';
  await refreshReviewDashboard();
}
async function setNeedsReviewOnly() {
  q('labelReviewStatus').value = 'needs_review';
  await refreshReviewDashboard();
}
async function clearFilters() {
  setScope('', '', '', '');
  q('keyword').value = '';
  q('labelReviewStatus').value = 'all';
  await refreshReviewDashboard();
}
function applyInitialQueryParams() {
  const search = new URLSearchParams(window.location.search);
  const hasExplicitScope = ['major_code', 'middle_code', 'small_code', 'sub_code'].some(key => search.has(key));
  for (const [id, key] of [
    ['majorCode', 'major_code'],
    ['middleCode', 'middle_code'],
    ['smallCode', 'small_code'],
    ['subCode', 'sub_code'],
    ['keyword', 'keyword'],
    ['labelReviewStatus', 'label_review_status'],
    ['limit', 'limit'],
  ]) {
    if (search.has(key)) q(id).value = search.get(key) || '';
  }
  if (!hasExplicitScope) setScope('02', '02', '02', '01');
}
applyInitialQueryParams();
refreshReviewDashboard();
</script>
</body>
</html>
""".replace("__DASHBOARD_DEFAULT_REVIEWER__", html.escape(default_dashboard_reviewer_id()))
    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KSA Review Dashboard</title>
  <style>
    :root { --ink:#172033; --muted:#667085; --line:#d9e0ea; --bg:#f6f8fb; --panel:#fff; --accent:#0f766e; --ok:#15803d; --warn:#b45309; --bad:#b91c1c; --blue:#1d4ed8; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Segoe UI, Arial, "Malgun Gothic", sans-serif; background:var(--bg); color:var(--ink); line-height:1.45; }
    main { max-width:1480px; margin:0 auto; padding:24px; }
    h1 { margin:0 0 6px; font-size:28px; }
    h2 { margin:0 0 10px; font-size:18px; }
    .muted { color:var(--muted); }
    .topbar { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom:14px; }
    .top-actions { display:flex; flex-wrap:wrap; gap:8px; justify-content:flex-end; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; margin:14px 0; }
    .toolbar { display:flex; flex-wrap:wrap; gap:8px; align-items:end; }
    .field { display:grid; gap:4px; min-width:120px; }
    label { font-size:12px; font-weight:700; color:#445166; }
    input, select { min-height:38px; border:1px solid #cbd5e1; border-radius:6px; padding:8px 10px; font:inherit; background:#fff; }
    input.code { width:72px; }
    input.keyword { min-width:260px; }
    button, a.button-link { min-height:38px; border:1px solid var(--accent); border-radius:6px; background:var(--accent); color:#fff; padding:8px 12px; font-weight:700; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; gap:7px; white-space:nowrap; }
    button.secondary, a.button-link.secondary { background:#fff; color:var(--accent); }
    button.good { border-color:var(--ok); background:var(--ok); }
    button.bad { border-color:var(--bad); background:var(--bad); }
    button:disabled { opacity:.45; cursor:not-allowed; }
    .button-icon { width:18px; min-width:18px; text-align:center; line-height:1; }
    .status { min-height:24px; margin-top:10px; font-weight:700; }
    .status.error { color:var(--bad); }
    .review-summary { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:12px; background:#fbfdff; min-height:92px; display:grid; gap:4px; }
    .metric span { color:var(--muted); font-size:12px; font-weight:700; }
    .metric strong { font-size:24px; }
    .metric.good strong { color:var(--ok); }
    .metric.warn strong { color:var(--warn); }
    .metric.bad strong { color:var(--bad); }
    .progress { height:10px; border-radius:999px; background:#e2e8f0; overflow:hidden; }
    .progress span { display:block; height:100%; background:var(--accent); border-radius:999px; }
    .scope-layout { display:grid; grid-template-columns:minmax(0,1.3fr) minmax(320px,.7fr); gap:14px; align-items:start; }
    .major-grid { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:8px; margin-top:12px; }
    .major-tile { width:100%; min-height:86px; border:1px solid var(--line); border-radius:8px; background:#fff; color:var(--ink); text-align:left; padding:10px; display:grid; gap:5px; cursor:pointer; }
    .node { width:100%; min-height:52px; border:1px solid var(--line); border-radius:8px; background:#fff; color:var(--ink); text-align:left; padding:9px 10px; display:flex; gap:9px; align-items:flex-start; cursor:pointer; }
    .major-tile.active, .node.active { border-color:var(--accent); background:#ecfdf5; box-shadow:0 0 0 2px rgba(15,118,110,.12); }
    .major-icon { width:34px; height:34px; border-radius:8px; display:flex; align-items:center; justify-content:center; background:#eefcf8; font-size:22px; }
    .node-icon { min-width:34px; min-height:34px; border-radius:8px; display:flex; align-items:center; justify-content:center; background:#eef2f7; color:#334155; font-size:12px; font-weight:800; }
    .node-main { min-width:0; flex:1; }
    .tile-title, .node-title { display:block; font-weight:700; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .tile-meta, .node-sub { display:block; color:var(--muted); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .taxonomy-columns { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-top:12px; }
    .taxonomy-column { border:1px solid var(--line); border-radius:8px; background:#fbfdff; padding:10px; min-height:170px; }
    .taxonomy-column header { display:flex; justify-content:space-between; gap:8px; align-items:center; margin-bottom:8px; }
    .taxonomy-list { display:grid; gap:6px; max-height:230px; overflow:auto; padding-right:2px; }
    .scope-progress { border:1px solid var(--line); border-radius:8px; background:#f8fafc; padding:12px; display:grid; gap:8px; }
    .scope-progress strong { font-size:20px; }
    .scroll { overflow:auto; max-height:68vh; border:1px solid var(--line); border-radius:8px; }
    table { width:100%; border-collapse:collapse; background:#fff; table-layout:fixed; }
    th, td { border-bottom:1px solid var(--line); padding:9px; text-align:left; vertical-align:top; font-size:13px; overflow-wrap:anywhere; }
    th { position:sticky; top:0; background:#eef2f5; z-index:1; }
    .pill { display:inline-flex; align-items:center; gap:5px; border:1px solid var(--line); border-radius:999px; padding:3px 8px; margin:0 4px 4px 0; background:#fff; font-size:12px; }
    .pill.good { border-color:#22c55e; color:#166534; background:#f0fdf4; }
    .pill.warn { border-color:#f59e0b; color:#92400e; background:#fffbeb; }
    .pill.bad { border-color:#ef4444; color:#991b1b; background:#fef2f2; }
    .review-actions { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
    .review-actions button { min-height:30px; padding:4px 8px; font-size:12px; }
    .raw { font-weight:700; }
    .label-candidate { display:grid; gap:6px; border:1px solid #bbf7d0; background:#f7fff9; border-radius:8px; padding:9px; }
    .label-candidate strong { line-height:1.45; }
    .definition-candidate { display:grid; gap:6px; border:1px solid #c7d2fe; background:#f8faff; border-radius:8px; padding:9px; }
    .definition-candidate strong { line-height:1.45; }
    .definition-source { font-size:12px; color:var(--muted); }
    .definition-evidence { margin-top:4px; color:var(--muted); font-size:12px; line-height:1.45; }
    .definition-notice { margin-top:10px; border:1px solid #c7d2fe; background:#f8faff; border-radius:8px; padding:10px 12px; display:grid; gap:5px; }
    .definition-notice strong { font-size:16px; }
    .ops-table { margin-top:12px; overflow:auto; max-height:260px; border:1px solid var(--line); border-radius:8px; }
    .ops-table table { min-width:900px; }
    @media (max-width:1120px) { .review-summary { grid-template-columns:repeat(3,minmax(0,1fr)); } .scope-layout, .taxonomy-columns { grid-template-columns:1fr; } .major-grid { grid-template-columns:repeat(3,minmax(0,1fr)); } table { min-width:1100px; } }
    @media (max-width:680px) { main { padding:12px; } .topbar { display:grid; } .review-summary, .major-grid { grid-template-columns:1fr; } input.keyword { min-width:100%; } .field, button, a.button-link { width:100%; } }
  </style>
</head>
<body>
<main>
  <div class="topbar">
    <div>
      <h1>KSA Review Dashboard</h1>
      <p class="muted">LLM 검토를 통과한 단어형 KSA 라벨 후보를 먼저 확인하는 전용 화면입니다.</p>
    </div>
  </div>

  <section class="panel">
    <div class="toolbar">
      <div class="field"><label>대분류</label><input id="majorCode" class="code" value=""></div>
      <div class="field"><label>중분류</label><input id="middleCode" class="code" value=""></div>
      <div class="field"><label>소분류</label><input id="smallCode" class="code" value=""></div>
      <div class="field"><label>세분류</label><input id="subCode" class="code" value=""></div>
      <div class="field"><label>키워드</label><input id="keyword" class="keyword" placeholder="KSA, 능력단위, 개념명"></div>
      <div class="field">
        <label>라벨 리뷰</label>
        <select id="labelReviewStatus">
          <option value="all">전체</option>
          <option value="llm_reviewed" selected>LLM 단어형 검토됨</option>
          <option value="candidate">라벨 전처리 후보(candidate)</option>
          <option value="needs_review">수정필요</option>
          <option value="human_reviewed">사람확인</option>
          <option value="rejected">거절</option>
          <option value="missing">후보 없음</option>
        </select>
      </div>
      <div class="field">
        <label>정의 리뷰</label>
        <select id="meaningReviewStatus">
          <option value="all" selected>전체</option>
          <option value="candidate">정의 문장 후보</option>
          <option value="needs_review">수정필요</option>
          <option value="human_reviewed">사람확인</option>
          <option value="missing">후보 없음</option>
        </select>
      </div>
      <div class="field"><label>표시</label><input id="limit" class="code" value="50"></div>
      <div class="field"><label>Review note</label><input id="reviewNote" class="keyword" placeholder="human review note"></div>
      <div class="field"><label>Decision packet</label><input id="reviewSourcePacket" class="keyword" placeholder="reports/...csv#label:123:approve"></div>
      <div class="field"><label>Packet SHA-256</label><input id="reviewSourceHash" class="keyword" placeholder="sha256:..."></div>
      <input id="reviewerId" type="hidden" value="__DASHBOARD_DEFAULT_REVIEWER__">
      <button onclick="refreshReviewDashboard()"><span class="button-icon" aria-hidden="true">🔎</span>조회</button>
      <button class="secondary" onclick="setHrScope()"><span class="button-icon" aria-hidden="true">🏢</span>인사 직무</button>
      <button class="secondary" onclick="setDefinitionPendingOnly()"><span class="button-icon" aria-hidden="true">☑</span>정의 문장 후보</button>
      <button class="secondary" onclick="setLabelPendingOnly()"><span class="button-icon" aria-hidden="true">☑</span>LLM 단어형 라벨</button>
      <button class="secondary" onclick="clearFilters()"><span class="button-icon" aria-hidden="true">↺</span>전체</button>
    </div>
    <div id="status" class="status muted"></div>
    <div id="labelCandidateNotice" class="definition-notice">
      <strong>단어형 KSA 라벨 후보를 불러오는 중입니다.</strong>
      <span class="muted">라벨 후보는 원문 KSA를 짧게 보이도록 만든 표시명 후보입니다. 원문 KSA는 수정하지 않습니다.</span>
    </div>
    <div id="definitionCandidateNotice" class="definition-notice">
      <strong>KSA 정의 문장 후보를 불러오는 중입니다.</strong>
      <span class="muted">정의 문장 후보는 단어형 라벨에 붙일 설명 후보이며 원문 KSA와 분리해 저장됩니다. LLM 단어형 검토는 정의 문장 확정이 아니라 라벨 후보의 자동 점검 상태입니다.</span>
    </div>
  </section>

  <section class="panel">
    <h2>리뷰 현황</h2>
    <div id="reviewSummary" class="review-summary"></div>
  </section>

  <section class="panel">
    <div class="scope-layout">
      <div>
        <h2>분류 선택</h2>
        <div class="muted">대분류부터 세분류까지 선택하면 같은 범위의 리뷰 진행률과 검토 항목만 다시 조회합니다.</div>
        <div id="majorTiles" class="major-grid"></div>
      </div>
      <div id="scopeProgress" class="scope-progress">
        <span class="muted">리뷰 범위를 선택하세요.</span>
      </div>
    </div>
    <div class="taxonomy-columns">
      <section class="taxonomy-column">
        <header><strong>중분류</strong><span id="middleMeta" class="muted"></span></header>
        <div id="middleList" class="taxonomy-list"><span class="muted">대분류를 선택하세요.</span></div>
      </section>
      <section class="taxonomy-column">
        <header><strong>소분류</strong><span id="smallMeta" class="muted"></span></header>
        <div id="smallList" class="taxonomy-list"><span class="muted">중분류를 선택하세요.</span></div>
      </section>
      <section class="taxonomy-column">
        <header><strong>세분류</strong><span id="subMeta" class="muted"></span></header>
        <div id="subList" class="taxonomy-list"><span class="muted">소분류를 선택하세요.</span></div>
      </section>
    </div>
  </section>

  <section class="panel">
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th style="width:300px;">단어형 KSA</th>
            <th style="width:340px;">원문 KSA</th>
            <th style="width:300px;">정의 후보</th>
            <th style="width:240px;">현재 상태</th>
            <th style="width:260px;">사람확인 버튼</th>
          </tr>
        </thead>
        <tbody id="reviewItems">
          <tr><td colspan="5" class="muted">조회 버튼을 눌러 리뷰 항목을 불러오세요.</td></tr>
        </tbody>
      </table>
    </div>
  </section>
</main>
<script>
const q = id => document.getElementById(id);
const fmt = new Intl.NumberFormat('ko-KR');
const majorIcons = {
  '01':'📊','02':'🧾','03':'🏦','04':'🎓','05':'⚖️','06':'🏥',
  '07':'🤝','08':'🎨','09':'🚚','10':'🏷️','11':'🧹','12':'🏨',
  '13':'🍽️','14':'🏗️','15':'⚙️','16':'🧱','17':'🧪','18':'🧵',
  '19':'⚡','20':'📡','21':'🥫','22':'🖨️','23':'🌱','24':'🚜'
};
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function jsArg(value) {
  return JSON.stringify(String(value ?? ''));
}
function toNum(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? number : 0;
}
function progressBar(percent) {
  const value = Math.max(0, Math.min(100, toNum(percent)));
  return `<div class="progress"><span style="width:${value.toFixed(1)}%"></span></div>`;
}
function params() {
  const p = new URLSearchParams();
  for (const [id, key] of [
    ['majorCode', 'major_code'],
    ['middleCode', 'middle_code'],
    ['smallCode', 'small_code'],
    ['subCode', 'sub_code'],
    ['keyword', 'keyword'],
    ['labelReviewStatus', 'label_review_status'],
    ['meaningReviewStatus', 'meaning_review_status'],
    ['limit', 'limit'],
  ]) {
    const value = q(id).value.trim();
    if (value && value !== 'all') p.set(key, value);
  }
  return p;
}
function selectedCodes() {
  return {
    major: q('majorCode').value.trim(),
    middle: q('middleCode').value.trim(),
    small: q('smallCode').value.trim(),
    sub: q('subCode').value.trim(),
  };
}
function setScope(major='', middle='', small='', sub='') {
  q('majorCode').value = major || '';
  q('middleCode').value = middle || '';
  q('smallCode').value = small || '';
  q('subCode').value = sub || '';
}
function taxonomyParams(level) {
  const p = new URLSearchParams();
  const codes = selectedCodes();
  p.set('level', level);
  p.set('limit', level === 'major' ? '100' : '500');
  if (codes.major) p.set('major_code', codes.major);
  if (codes.middle) p.set('middle_code', codes.middle);
  if (codes.small) p.set('small_code', codes.small);
  return p;
}
async function fetchJson(path, options={}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok || data.ok === false) throw new Error(data.error || (data.error && data.error.detail) || 'request failed');
  return data;
}
function nodeName(target) {
  const el = q(target)?.querySelector('.active .node-title, .active .tile-title');
  return el ? el.textContent : '';
}
function currentScopeLabel() {
  const names = [nodeName('majorTiles'), nodeName('middleList'), nodeName('smallList'), nodeName('subList')].filter(Boolean);
  const codes = selectedCodes();
  const codeText = [codes.major, codes.middle, codes.small, codes.sub].filter(Boolean).join('-') || '전체 NCS';
  return names.length ? `${names.join(' > ')} (${codeText})` : codeText;
}
function renderMajorTiles(nodes) {
  const codes = selectedCodes();
  q('majorTiles').innerHTML = (nodes || []).map(node => {
    const active = node.major_code === codes.major;
    return `<button type="button" class="major-tile${active ? ' active' : ''}" onclick="selectMajor('${esc(node.major_code)}')">
      <div class="major-icon" aria-hidden="true">${majorIcons[node.major_code] || esc(node.major_code)}</div>
      <span class="tile-title">${esc(node.major_code)}. ${esc(node.name || '')}</span>
      <span class="tile-meta">리뷰 범위 선택</span>
    </button>`;
  }).join('');
}
function renderNodeList(target, metaTarget, nodes, level) {
  const codes = selectedCodes();
  q(metaTarget).textContent = nodes && nodes.length ? `${fmt.format(nodes.length)}개` : '';
  if (!nodes || !nodes.length) {
    const messages = {middle:'대분류를 선택하세요.', small:'중분류를 선택하세요.', sub:'소분류를 선택하세요.'};
    q(target).innerHTML = `<span class="muted">${esc(messages[level] || '결과가 없습니다.')}</span>`;
    return;
  }
  q(target).innerHTML = nodes.map(node => {
    const active =
      (level === 'middle' && node.middle_code === codes.middle) ||
      (level === 'small' && node.small_code === codes.small) ||
      (level === 'sub' && node.sub_code === codes.sub);
    const click =
      level === 'middle'
        ? `selectMiddle('${esc(node.major_code)}','${esc(node.middle_code)}')`
        : level === 'small'
          ? `selectSmall('${esc(node.major_code)}','${esc(node.middle_code)}','${esc(node.small_code)}')`
          : `selectSub('${esc(node.major_code)}','${esc(node.middle_code)}','${esc(node.small_code)}','${esc(node.sub_code)}')`;
    return `<button type="button" class="node${active ? ' active' : ''}" onclick="${click}">
      <span class="node-icon">${esc(node.code || '')}</span>
      <span class="node-main">
        <span class="node-title">${esc(node.name || '')}</span>
        <span class="node-sub">리뷰 범위 선택</span>
      </span>
    </button>`;
  }).join('');
}
async function loadTaxonomy() {
  const codes = selectedCodes();
  const majors = await fetchJson('/api/taxonomy?' + taxonomyParams('major').toString());
  const middles = codes.major ? await fetchJson('/api/taxonomy?' + taxonomyParams('middle').toString()) : {nodes:[]};
  const smalls = codes.major && codes.middle ? await fetchJson('/api/taxonomy?' + taxonomyParams('small').toString()) : {nodes:[]};
  const subs = codes.major && codes.middle && codes.small ? await fetchJson('/api/taxonomy?' + taxonomyParams('sub').toString()) : {nodes:[]};
  renderMajorTiles(majors.nodes || []);
  renderNodeList('middleList', 'middleMeta', middles.nodes || [], 'middle');
  renderNodeList('smallList', 'smallMeta', smalls.nodes || [], 'small');
  renderNodeList('subList', 'subMeta', subs.nodes || [], 'sub');
}
async function selectMajor(major) { setScope(major, '', '', ''); await refreshReviewDashboard(); }
async function selectMiddle(major, middle) { setScope(major, middle, '', ''); await refreshReviewDashboard(); }
async function selectSmall(major, middle, small) { setScope(major, middle, small, ''); await refreshReviewDashboard(); }
async function selectSub(major, middle, small, sub) { setScope(major, middle, small, sub); await refreshReviewDashboard(); }
function metric(label, value, detail, cls='') {
  return `<div class="metric ${esc(cls)}"><span>${esc(label)}</span><strong>${esc(value)}</strong><div class="muted">${esc(detail || '')}</div></div>`;
}
function renderSummary(data) {
  const p = data.label_review_progress || {};
  const total = toNum(p.total);
  const human = toNum(p.human_reviewed);
  const pending = toNum(p.pending);
  const needs = toNum(p.needs_review);
  const rejected = toNum(p.rejected);
  const humanPercent = toNum(p.human_reviewed_percent);
  const remainingHuman = Math.max(0, total - human);
  const reviewQueue = pending + needs;
  const unitLabel = p.unit_label === 'Broad representative concepts' ? '대표 개념 후보' : (p.unit_label || 'KSA 후보');
  q('reviewSummary').innerHTML = [
    metric('전체 리뷰 대상', fmt.format(total), unitLabel),
    metric('사람확인', `${humanPercent.toFixed(1)}%`, `${fmt.format(human)} / ${fmt.format(total)}`, human ? 'good' : 'warn'),
    metric('남은 검토', fmt.format(remainingHuman), '사람확인 필요', remainingHuman ? 'warn' : 'good'),
    metric('전처리 후보', fmt.format(pending), '단어형 후보 생성 후 사람확인 대기', pending ? 'warn' : 'good'),
    metric('수정필요', fmt.format(needs), '재검토 대상', needs ? 'bad' : 'good'),
    metric('거절', fmt.format(rejected), '사용 제외 후보', rejected ? 'warn' : 'good'),
  ].join('');
  q('scopeProgress').innerHTML = `
    <span class="muted">현재 리뷰 범위</span>
    <strong>${esc(currentScopeLabel())}</strong>
    <div><b>사람확인</b> ${fmt.format(human)} / ${fmt.format(total)} (${humanPercent.toFixed(1)}%)</div>
    ${progressBar(humanPercent)}
    <div><b>검토대상</b> ${fmt.format(reviewQueue)}건 · 남은 검토 ${fmt.format(remainingHuman)}건</div>
  `;
}
function renderSummary(data) {
  const lp = data.label_review_progress || {};
  const mp = data.meaning_review_progress || {};
  const labelTotal = toNum(lp.total);
  const labelHuman = toNum(lp.human_reviewed);
  const labelPending = toNum(lp.pending);
  const labelNeeds = toNum(lp.needs_review);
  const labelRejected = toNum(lp.rejected);
  const labelHumanPercent = toNum(lp.human_reviewed_percent);
  const definitionTotal = toNum(mp.total);
  const definitionHuman = toNum(mp.human_reviewed);
  const definitionPending = toNum(mp.pending);
  const definitionNeeds = toNum(mp.needs_review);
  const definitionRejected = toNum(mp.rejected);
  const definitionHumanPercent = toNum(mp.human_reviewed_percent);
  const labelQueue = labelPending + labelNeeds;
  const definitionQueue = definitionPending + definitionNeeds;
  q('reviewSummary').innerHTML = [
    metric('라벨 리뷰율', `${labelHumanPercent.toFixed(1)}%`, `${fmt.format(labelHuman)} / ${fmt.format(labelTotal)}`, labelHuman ? 'good' : 'warn'),
    metric('정의 리뷰율', `${definitionHumanPercent.toFixed(1)}%`, `${fmt.format(definitionHuman)} / ${fmt.format(definitionTotal)}`, definitionHuman ? 'good' : 'warn'),
    metric('라벨 검토대기', fmt.format(labelQueue), `미확정 ${fmt.format(labelPending)} / 수정필요 ${fmt.format(labelNeeds)}`, labelQueue ? 'warn' : 'good'),
    metric('정의 검토대기', fmt.format(definitionQueue), `미확정 ${fmt.format(definitionPending)} / 수정필요 ${fmt.format(definitionNeeds)}`, definitionQueue ? 'warn' : 'good'),
    metric('라벨 거절', fmt.format(labelRejected), '제외 후보', labelRejected ? 'warn' : 'good'),
    metric('정의 거절', fmt.format(definitionRejected), '제외 후보', definitionRejected ? 'warn' : 'good'),
  ].join('');
  q('scopeProgress').innerHTML = `
    <span class="muted">현재 리뷰 범위</span>
    <strong>${esc(currentScopeLabel())}</strong>
    <div><b>라벨 사람확인</b> ${fmt.format(labelHuman)} / ${fmt.format(labelTotal)} (${labelHumanPercent.toFixed(1)}%)</div>
    ${progressBar(labelHumanPercent)}
    <div><b>정의 사람확인</b> ${fmt.format(definitionHuman)} / ${fmt.format(definitionTotal)} (${definitionHumanPercent.toFixed(1)}%)</div>
    ${progressBar(definitionHumanPercent)}
    <div><b>검토대기</b> 라벨 ${fmt.format(labelQueue)}건 · 정의 ${fmt.format(definitionQueue)}건</div>
  `;
}
function hasScopedMeaningEvidence(item) {
  return Boolean(item && item.ksa_id && (item.unit_code || item.element_id || item.criteria_ids?.length || item.related_criteria_count || item.task_evidence_count));
}
function renderLabelCandidateNotice(data, visibleCount) {
  const lp = data.label_review_progress || {};
  const total = toNum(lp.total);
  const candidate = toNum(lp.pending);
  const autoChecked = toNum(lp.llm_reviewed);
  const human = toNum(lp.human_reviewed);
  const needs = toNum(lp.needs_review);
  const rejected = toNum(lp.rejected);
  const filter = q('labelReviewStatus').value || 'all';
  const filterLabel = {
    all: '전체',
    llm_reviewed: 'LLM 단어형 검토됨 · 자동 추출 결과(미승인)',
    candidate: '라벨 전처리 후보',
    needs_review: '수정필요',
    human_reviewed: '사람확인',
    rejected: '거절',
    missing: '후보 없음',
  }[filter] || filter;
  q('labelCandidateNotice').innerHTML = `
    <strong>단어형 KSA 라벨 후보 ${fmt.format(total)}건 · 현재 화면 ${fmt.format(visibleCount)}건</strong>
    <span>현재 라벨 리뷰 필터: <b>${esc(filterLabel)}</b> · LLM 단어형 검토됨 ${fmt.format(autoChecked)} / 단어형 라벨 후보 ${fmt.format(candidate)} / 수정필요 ${fmt.format(needs)} / 사람확인 ${fmt.format(human)} / 거절 ${fmt.format(rejected)}</span>
    <span class="muted">LLM 단어형 검토는 후보가 자동 점검을 통과했다는 뜻입니다. 원문 KSA를 바꾼 것이 아니며 사람 승인도 아닙니다. 화면의 단어형 KSA 라벨 후보는 자동 추출 결과입니다.</span>
  `;
}
function renderDefinitionCandidateNotice(data, visibleCount) {
  const mp = data.meaning_review_progress || {};
  const total = toNum(mp.total);
  const pending = toNum(mp.pending);
  const human = toNum(mp.human_reviewed);
  const needs = toNum(mp.needs_review);
  const rejected = toNum(mp.rejected);
  const filter = q('meaningReviewStatus').value || 'all';
  const filterLabel = {
    all: '전체',
    candidate: '정의 문장 후보',
    needs_review: '수정필요',
    human_reviewed: '사람확인',
    rejected: '거절',
    missing: '후보 없음',
  }[filter] || filter;
  q('definitionCandidateNotice').innerHTML = `
    <strong>KSA 정의 문장 후보 ${fmt.format(total)}건 · 현재 화면 ${fmt.format(visibleCount)}건</strong>
    <span>현재 정의 리뷰 필터: <b>${esc(filterLabel)}</b> · 정의 문장 후보 ${fmt.format(pending)} / 사람확인 ${fmt.format(human)} / 수정필요 ${fmt.format(needs)} / 거절 ${fmt.format(rejected)}</span>
    <span class="muted">정의 문장 후보는 전처리 미실행이 아니라 LLM/규칙으로 생성된 뒤 아직 사람이 확인하지 않은 설명 후보라는 뜻입니다. 단어형 LLM review 상태는 왼쪽 라벨 후보 상태에서 확인합니다.</span>
  `;
}
function renderRows(items) {
  if (!items.length) {
    const labelFilter = q('labelReviewStatus').value || 'all';
    const meaningFilter = q('meaningReviewStatus').value || 'all';
    const hint = labelFilter === 'llm_reviewed'
      ? '현재 조건의 LLM 단어형 라벨 후보가 없습니다. 분류를 넓히거나 라벨 리뷰 필터를 전체로 바꿔보세요.'
      : (meaningFilter === 'candidate'
        ? '현재 정의 문장 후보가 없습니다. 이미 확인한 항목은 정의 리뷰 필터를 전체 또는 사람확인으로 바꾸면 볼 수 있습니다.'
        : '현재 조건의 리뷰 항목이 없습니다. 분류나 리뷰 필터를 바꿔보세요.');
    q('reviewItems').innerHTML = `<tr><td colspan="5" class="muted">${esc(hint)}</td></tr>`;
    return;
  }
  q('reviewItems').innerHTML = items.map(item => {
    const labelStatus = item.short_label_review_status || 'missing';
    const labelStatusLabel = {
      llm_reviewed: 'LLM 단어형 검토됨 · 자동 추출 결과(미승인)',
      candidate: '단어형 라벨 후보',
      needs_review: '수정필요',
      human_reviewed: '사람확인',
      rejected: '거절',
      missing: '후보 없음',
    }[labelStatus] || labelStatus;
    const labelStatusClass = labelStatus === 'human_reviewed'
      ? 'good'
      : (labelStatus === 'needs_review' || labelStatus === 'rejected' ? 'bad' : 'warn');
    const shortLabelBlock = item.short_label_candidate
      ? `<div class="label-candidate">
          <span class="pill good">단어형 KSA 라벨 후보</span>
          <strong>${esc(item.short_label_candidate)}</strong>
          <span class="pill ${esc(labelStatusClass)}">${esc(labelStatusLabel)}</span>
          <span class="definition-source">상태 ${esc(labelStatusLabel)}</span>
          ${labelStatus === 'llm_reviewed' ? '<span class="definition-source">LLM 검토를 통과한 단어형 라벨 자동 추출 결과입니다. 사람 승인 상태가 아니며, 최종 확정이 필요할 때만 라벨 사람확인을 누르세요.</span>' : ''}
        </div>`
      : `<span class="muted">단어형 라벨 후보 없음</span>`;
    const termDefinitionStatus = item.term_definition_review_status || item.meaning_review_status || 'missing';
    const meaningStatusLabel = {
      candidate: 'LLM 전처리됨 · 자동 추출 결과(미승인) · 사람확인 대기',
      llm_reviewed: 'LLM 단어형 검토됨 · 자동 추출 결과(미승인)',
      needs_review: '수정필요',
      human_reviewed: '사람확인',
      rejected: '거절',
      missing: '후보 없음',
    }[termDefinitionStatus] || termDefinitionStatus;
    const meaningStatusClass = termDefinitionStatus === 'human_reviewed'
      ? 'good'
      : (termDefinitionStatus === 'needs_review' || termDefinitionStatus === 'rejected' ? 'bad' : 'warn');
    const definitionStatusLabel = {
      missing: '정의 없음',
      candidate: '정의 후보',
      defined: '정의 있음',
      human_reviewed: '정의 사람확인',
    }[item.definition_status || ''] || '정의 없음';
    const termDefinitionBlock = item.term_definition_candidate
      ? `<details class="definition-candidate">
          <summary>KSA 정의 문장 후보 보기</summary>
          <div class="definition-evidence">${esc(item.term_definition_candidate)}</div>
          <span class="pill ${esc(meaningStatusClass)}">${esc(meaningStatusLabel)}</span>
        </details>`
      : `<span class="muted">정의 문장 후보 없음</span>`;
    const labelButtons = item.short_label_id
      ? `<div class="review-actions">
          <button class="good" onclick="reviewShortLabel(${Number(item.short_label_id)}, 'approve')">라벨 사람확인</button>
          <button class="secondary" onclick="reviewShortLabel(${Number(item.short_label_id)}, 'needs_revision')">라벨 수정필요</button>
          <button class="bad" onclick="reviewShortLabel(${Number(item.short_label_id)}, 'reject')">라벨 거절</button>
        </div>`
      : '<span class="muted">라벨 후보 없음</span>';
    const meaningButtons = item.term_definition_meaning_id
      ? `<div class="review-actions">
          <button class="good" onclick="reviewMeaningCandidate(${Number(item.term_definition_meaning_id)}, 'approve')">정의 사람확인</button>
          <button class="secondary" onclick="reviewMeaningCandidate(${Number(item.term_definition_meaning_id)}, 'needs_revision')">정의 수정필요</button>
          <button class="bad" onclick="reviewMeaningCandidate(${Number(item.term_definition_meaning_id)}, 'reject')">정의 거절</button>
        </div>`
      : '';
    return `<tr>
      <td>${shortLabelBlock}</td>
      <td><div class="raw">${esc(item.ksa_text_raw || '')}</div><div class="muted">${esc(item.major_code || '')} ${esc(item.major_name || '')} / ${esc(item.unit_name_raw || '')}</div></td>
      <td>${termDefinitionBlock}</td>
      <td>
        <span class="pill ${esc(labelStatusClass)}">라벨 ${esc(labelStatusLabel)}</span>
        <span class="pill">${esc(definitionStatusLabel)}</span>
        <span class="pill ${esc(meaningStatusClass)}">정의 ${esc(meaningStatusLabel)}</span>
      </td>
      <td>${labelButtons}${meaningButtons}</td>
    </tr>`;
  }).join('');
}
async function loadReviewItems() {
  const data = await fetchJson('/api/ksa-definitions?' + params().toString());
  renderSummary(data);
  renderLabelCandidateNotice(data, (data.items || []).length);
  renderDefinitionCandidateNotice(data, (data.items || []).length);
  renderRows(data.items || []);
  q('status').className = 'status muted';
  q('status').textContent = `${fmt.format((data.items || []).length)}개 리뷰 항목을 불러왔습니다. 정의 확인 후 행이 사라지면 저장 후 필터에서 제외된 것입니다.`;
}
async function oneClickPayload(kind, targetId, decision) {
  const artifact = '/ksa-review-dashboard?' + params().toString();
  const packet = q('reviewSourcePacket').value.trim();
  const packetHash = q('reviewSourceHash').value.trim();
  const note = q('reviewNote').value.trim();
  if (!packet || !packetHash || !note) {
    throw new Error('사람확인/수정에는 review note, reports decision packet, packet SHA-256이 필요합니다.');
  }
  return {
    decision,
    reviewer_id: q('reviewerId').value || '',
    source_decision_packet: packet,
    source_artifact_hash: packetHash,
    run_artifact: artifact,
    notes: note,
    rationale: note,
  };
}
async function reviewShortLabel(labelId, decision) {
  q('status').className = 'status muted';
  q('status').textContent = '저장 중...';
  try {
    const payload = await oneClickPayload('label', labelId, decision);
    payload.label_id = labelId;
    payload.raw_to_label_checked = true;
    const data = await fetchJson('/api/ksa-label-review', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    q('status').textContent = `저장됨: 라벨 ${data.label_id} ${decision === 'approve' ? '사람확인' : (decision === 'needs_revision' ? '수정필요' : '거절')}`;
    await loadReviewItems();
  } catch (err) {
    q('status').className = 'status error';
    q('status').textContent = err.message;
  }
}
async function reviewMeaningCandidate(meaningId, decision) {
  q('status').className = 'status muted';
  q('status').textContent = '저장 중...';
  try {
    const payload = await oneClickPayload('meaning', meaningId, decision);
    payload.meaning_id = meaningId;
    payload.raw_to_meaning_checked = true;
    const data = await fetchJson('/api/ksa-meaning-review', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    q('status').textContent = `저장됨: 정의 ${data.meaning_id} ${decision === 'approve' ? '사람확인' : (decision === 'needs_revision' ? '수정필요' : '거절')}`;
    await loadReviewItems();
  } catch (err) {
    q('status').className = 'status error';
    q('status').textContent = err.message;
  }
}
async function refreshReviewDashboard() {
  q('status').className = 'status muted';
  q('status').textContent = 'Loading...';
  try {
    await loadTaxonomy();
    await loadReviewItems();
  } catch (err) {
    q('status').className = 'status error';
    q('status').textContent = err.message;
  }
}
async function setHrScope() {
  setScope('02', '02', '02', '01');
  await refreshReviewDashboard();
}
async function setDefinitionPendingOnly() {
  q('labelReviewStatus').value = 'all';
  q('meaningReviewStatus').value = 'candidate';
  await refreshReviewDashboard();
}
async function setLabelPendingOnly() {
  q('labelReviewStatus').value = 'llm_reviewed';
  q('meaningReviewStatus').value = 'all';
  await refreshReviewDashboard();
}
async function clearFilters() {
  setScope('', '', '', '');
  q('keyword').value = '';
  q('labelReviewStatus').value = 'all';
  q('meaningReviewStatus').value = 'all';
  await refreshReviewDashboard();
}
function applyInitialQueryParams() {
  const search = new URLSearchParams(window.location.search);
  const hasExplicitScope = ['major_code', 'middle_code', 'small_code', 'sub_code'].some(key => search.has(key));
  for (const [id, key] of [
    ['majorCode', 'major_code'],
    ['middleCode', 'middle_code'],
    ['smallCode', 'small_code'],
    ['subCode', 'sub_code'],
    ['keyword', 'keyword'],
    ['labelReviewStatus', 'label_review_status'],
    ['meaningReviewStatus', 'meaning_review_status'],
    ['limit', 'limit'],
  ]) {
    if (search.has(key)) q(id).value = search.get(key) || '';
  }
  if (!hasExplicitScope) setScope('02', '02', '02', '01');
}
applyInitialQueryParams();
refreshReviewDashboard();
</script>
</body>
</html>
""".replace("__DASHBOARD_DEFAULT_REVIEWER__", html.escape(default_dashboard_reviewer_id()))


def render_ksa_preprocessing_dashboard_html_legacy() -> str:
    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KSA Preprocessing Dashboard</title>
  <style>
    :root { --ink:#172033; --muted:#667085; --line:#d9e0ea; --bg:#f6f8fb; --panel:#fff; --accent:#0f766e; --ok:#15803d; --warn:#b45309; --bad:#b91c1c; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Segoe UI, Arial, "Malgun Gothic", sans-serif; background:var(--bg); color:var(--ink); line-height:1.45; }
    main { max-width:1280px; margin:0 auto; padding:24px; }
    h1 { margin:0 0 6px; font-size:26px; }
    h2 { margin:20px 0 10px; font-size:18px; }
    .muted { color:var(--muted); }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; margin:14px 0; }
    .toolbar { display:flex; flex-wrap:wrap; gap:8px; align-items:end; }
    .field { display:grid; gap:4px; min-width:120px; }
    label { font-size:12px; font-weight:700; color:#445166; }
    input, select { min-height:38px; border:1px solid #cbd5e1; border-radius:6px; padding:8px 10px; font:inherit; background:#fff; }
    input.code { width:72px; }
    input.keyword { min-width:260px; }
    button, a.button-link { min-height:38px; border:1px solid var(--accent); border-radius:6px; background:var(--accent); color:#fff; padding:8px 12px; font-weight:700; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; }
    button.secondary, a.button-link.secondary { background:#fff; color:var(--accent); }
    .status { min-height:24px; margin-top:10px; font-weight:700; }
    .status.error { color:var(--bad); }
    .cards { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:12px; background:#fbfdff; display:grid; gap:6px; min-height:108px; }
    .metric span { color:var(--muted); font-size:12px; }
    .metric strong { font-size:24px; }
    .metric.good strong { color:var(--ok); }
    .metric.warn strong { color:var(--warn); }
    .metric.bad strong { color:var(--bad); }
    .progress { height:10px; border-radius:999px; background:#e2e8f0; overflow:hidden; }
    .progress span { display:block; height:100%; background:var(--accent); border-radius:999px; }
    .pipeline { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; }
    .stage { border:1px solid var(--line); border-radius:8px; background:#fff; padding:12px; display:grid; gap:4px; }
    .stage b { font-size:14px; }
    .stage strong { font-size:22px; }
    .pill { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:2px 7px; margin:0 4px 4px 0; background:#fff; font-size:12px; }
    details { border:1px solid var(--line); border-radius:8px; background:#fbfdff; padding:10px 12px; }
    summary { cursor:pointer; font-weight:700; }
    @media (max-width:1040px) { .cards, .pipeline { grid-template-columns:repeat(2,minmax(0,1fr)); } }
    @media (max-width:680px) { main { padding:12px; } .cards, .pipeline { grid-template-columns:1fr; } input.keyword { min-width:100%; } .field { width:100%; } }
  </style>
</head>
<body>
<main>
  <h1>KSA 전처리 현황</h1>
  <p class="muted">휴먼 리뷰 화면과 분리한 읽기 전용 전처리 대시보드입니다. 현재 어느 단계까지 준비됐는지만 크게 보여줍니다.</p>

  <section class="panel">
    <div class="toolbar">
      <div class="field"><label>대분류</label><input id="majorCode" class="code" value=""></div>
      <div class="field"><label>중분류</label><input id="middleCode" class="code" value=""></div>
      <div class="field"><label>소분류</label><input id="smallCode" class="code" value=""></div>
      <div class="field"><label>세분류</label><input id="subCode" class="code" value=""></div>
      <div class="field"><label>키워드</label><input id="keyword" class="keyword" placeholder="능력단위, KSA, 개념명"></div>
      <div class="field"><label>Limit</label><input id="limit" class="code" value="1"></div>
      <button onclick="loadPreprocessing()">조회</button>
      <button class="secondary" onclick="setHrScope()">인사 직무</button>
      <button class="secondary" onclick="clearFilters()">전체</button>
      <a class="button-link secondary" href="/ksa-review-dashboard">휴먼 리뷰 화면</a>
      <a class="button-link secondary" href="/">Main Dashboard</a>
    </div>
    <div id="status" class="status muted"></div>
  </section>

  <section class="pipeline" id="pipeline"></section>
  <section class="panel" id="llmBacklogPanel">
    <h2>LLM Preprocessing Backlog</h2>
    <div class="cards" id="llmBacklogCards"></div>
    <div id="llmBacklogPolicy" class="muted"></div>
  </section>
  <section class="panel">
    <h2>핵심 진행률</h2>
    <div class="cards" id="cards"></div>
  </section>
  <section class="panel">
    <h2>다음 작업 판단</h2>
    <div id="nextSteps" class="muted"></div>
  </section>
  <section class="panel">
    <details>
      <summary>상세 카운트 보기</summary>
      <div id="counts" class="muted" style="margin-top:10px;"></div>
    </details>
  </section>
</main>
<script>
const q = id => document.getElementById(id);
const fmt = new Intl.NumberFormat('ko-KR');
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function params() {
  const p = new URLSearchParams();
  for (const [id, key] of [
    ['majorCode', 'major_code'],
    ['middleCode', 'middle_code'],
    ['smallCode', 'small_code'],
    ['subCode', 'sub_code'],
    ['keyword', 'keyword'],
    ['limit', 'limit'],
  ]) {
    const value = q(id).value.trim();
    if (value) p.set(key, value);
  }
  return p;
}
function progressBar(percent) {
  const value = Math.max(0, Math.min(100, Number(percent || 0)));
  return `<div class="progress"><span style="width:${value.toFixed(1)}%"></span></div>`;
}
function pct(done, total) {
  return total ? done * 100 / total : 0;
}
function metric(label, value, detail, percent, cls='') {
  return `<div class="metric ${esc(cls)}"><span>${esc(label)}</span><strong>${esc(value)}</strong><div class="muted">${esc(detail || '')}</div>${percent === null ? '' : progressBar(percent)}</div>`;
}
function stage(label, value, detail, cls='') {
  return `<div class="stage ${esc(cls)}"><b>${esc(label)}</b><strong>${esc(value)}</strong><span class="muted">${esc(detail || '')}</span></div>`;
}
function countMap(label, values) {
  const entries = Object.entries(values || {});
  if (!entries.length) return `<div><strong>${esc(label)}</strong> <span class="pill">none</span></div>`;
  return `<div><strong>${esc(label)}</strong> ${entries.map(([key, value]) => `<span class="pill">${esc(key)} ${fmt.format(Number(value || 0))}</span>`).join('')}</div>`;
}
function applyInitialQueryParams() {
  const search = new URLSearchParams(window.location.search);
  for (const [id, key] of [
    ['majorCode', 'major_code'],
    ['middleCode', 'middle_code'],
    ['smallCode', 'small_code'],
    ['subCode', 'sub_code'],
    ['keyword', 'keyword'],
  ]) {
    if (search.has(key)) q(id).value = search.get(key) || '';
  }
}
async function loadPreprocessing() {
  q('status').className = 'status muted';
  q('status').textContent = 'Loading...';
  try {
    const response = await fetch('/api/ksa-definitions?' + params().toString());
    const data = await response.json();
    if (!data.ok) throw new Error((data.error && data.error.detail) || 'KSA preprocessing dashboard failed');
    const s = data.summary || {};
    const labelProgress = data.label_review_progress || {};
    const matching = Number(s.matching_ksa || 0);
    const atomic = Number(s.atomic_preprocessed_ksa || 0);
    const linked = Number(s.linked_ksa || 0);
    const labelCandidates = Number(s.label_candidate_concepts || 0);
    const definitionCandidates = Number(s.candidate_definition_concepts || 0);
    const taskEvidence = Number(s.criteria_evidence_linked_ksa || 0);
    const labelTotal = Number(labelProgress.total || 0);
    const actioned = Number(labelProgress.actioned || 0);
    const human = Number(labelProgress.human_reviewed || 0);
    q('pipeline').innerHTML = [
      stage('1 원문 KSA', fmt.format(matching), '읽기 전용 원천'),
      stage('2 원자 KSA', fmt.format(atomic), `${pct(atomic, matching).toFixed(1)}%`, atomic ? 'good' : 'warn'),
      stage('3 개념 연결', fmt.format(linked), `${pct(linked, matching).toFixed(1)}%`, linked ? 'good' : 'warn'),
      stage('4 짧은 라벨', fmt.format(labelCandidates), '휴먼 리뷰 후보'),
      stage('5 정의 후보', fmt.format(definitionCandidates), 'boilerplate 자동 승격 금지', definitionCandidates ? 'warn' : ''),
      stage('6 과업 근거', fmt.format(taskEvidence), `${pct(taskEvidence, matching).toFixed(1)}%`, taskEvidence ? 'good' : 'warn'),
    ].join('');
    q('cards').innerHTML = [
      metric('원자 KSA 생성률', `${pct(atomic, matching).toFixed(1)}%`, `${fmt.format(atomic)} / ${fmt.format(matching)}`, pct(atomic, matching), atomic ? 'good' : 'warn'),
      metric('개념 연결률', `${pct(linked, matching).toFixed(1)}%`, `${fmt.format(linked)} / ${fmt.format(matching)}`, pct(linked, matching), linked ? 'good' : 'warn'),
      metric('과업 근거 연결률', `${pct(taskEvidence, matching).toFixed(1)}%`, `${fmt.format(taskEvidence)} / ${fmt.format(matching)}`, pct(taskEvidence, matching), taskEvidence ? 'good' : 'warn'),
      metric('라벨 정리 진행률', `${Number(labelProgress.actioned_percent || 0).toFixed(1)}%`, `${fmt.format(actioned)} / ${fmt.format(labelTotal)}`, Number(labelProgress.actioned_percent || 0), actioned ? 'good' : 'warn'),
      metric('사람 확인률', `${Number(labelProgress.human_reviewed_percent || 0).toFixed(1)}%`, `${fmt.format(human)} / ${fmt.format(labelTotal)}`, Number(labelProgress.human_reviewed_percent || 0), human ? 'good' : 'warn'),
      metric('표시 기준', data.label_review_progress?.unit_label || 'Filtered KSA rows', `조회 행 ${fmt.format((data.items || []).length)}`, null),
    ].join('');
    const blockers = [];
    if (Number(s.unlinked_ksa || 0)) blockers.push(`개념 미연결 KSA ${fmt.format(Number(s.unlinked_ksa || 0))}건`);
    if (Number(labelProgress.pending || 0)) blockers.push(`라벨 대기 ${fmt.format(Number(labelProgress.pending || 0))}건`);
    if (Number(labelProgress.needs_review || 0)) blockers.push(`수정필요 ${fmt.format(Number(labelProgress.needs_review || 0))}건`);
    if (Number(s.generic_label_candidate_concepts || 0)) blockers.push(`너무 범용적인 라벨 ${fmt.format(Number(s.generic_label_candidate_concepts || 0))}개념`);
    q('nextSteps').innerHTML = blockers.length
      ? blockers.map(item => `<span class="pill">${esc(item)}</span>`).join('')
      : '<span class="pill">현재 조회 범위에서 큰 전처리 경고가 없습니다.</span>';
    q('counts').innerHTML = [
      countMap('definition_status', data.definition_status_counts),
      countMap('concept_review_status', data.concept_review_status_counts),
      countMap('label_review_status', data.label_review_status_counts),
      countMap('meaning_review_status', data.meaning_review_status_counts),
      countMap('label_quality_flags', data.label_quality_flag_counts),
    ].join('');
    q('status').textContent = `${fmt.format(matching)}개 KSA 기준 전처리 현황을 불러왔습니다.`;
  } catch (err) {
    q('status').className = 'status error';
    q('status').textContent = err.message;
  }
}
function setHrScope() {
  q('majorCode').value = '02';
  q('middleCode').value = '02';
  q('smallCode').value = '02';
  q('subCode').value = '01';
  loadPreprocessing();
}
function clearFilters() {
  for (const id of ['majorCode', 'middleCode', 'smallCode', 'subCode', 'keyword']) q(id).value = '';
  q('limit').value = '1';
  loadPreprocessing();
}
applyInitialQueryParams();
loadPreprocessing();
</script>
</body>
</html>
"""


def render_ksa_preprocessing_dashboard_html() -> str:
    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>KSA Preprocessing Dashboard</title>
  <style>
    :root {
      --ink:#172033;
      --muted:#667085;
      --line:#d9e0ea;
      --bg:#f6f8fb;
      --panel:#fff;
      --accent:#0f766e;
      --accent-soft:#ecfdf5;
      --ok:#15803d;
      --warn:#b45309;
      --bad:#b91c1c;
      --blue:#1d4ed8;
    }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Segoe UI, Arial, "Malgun Gothic", sans-serif; background:var(--bg); color:var(--ink); line-height:1.45; }
    main { max-width:1480px; margin:0 auto; padding:24px; }
    h1 { margin:0 0 6px; font-size:28px; letter-spacing:0; }
    h2 { margin:0 0 10px; font-size:18px; letter-spacing:0; }
    h3 { margin:0; font-size:15px; letter-spacing:0; }
    .muted { color:var(--muted); }
    .topbar { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; margin-bottom:14px; }
    .top-actions { display:flex; flex-wrap:wrap; gap:8px; justify-content:flex-end; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; margin:14px 0; }
    .toolbar { display:flex; flex-wrap:wrap; gap:8px; align-items:end; }
    .field { display:grid; gap:4px; min-width:120px; }
    label { font-size:12px; font-weight:700; color:#445166; }
    input, select { min-height:38px; border:1px solid #cbd5e1; border-radius:6px; padding:8px 10px; font:inherit; background:#fff; }
    input.code { width:72px; }
    input.keyword { min-width:260px; }
    button, a.button-link {
      min-height:38px;
      border:1px solid var(--accent);
      border-radius:6px;
      background:var(--accent);
      color:#fff;
      padding:8px 12px;
      font-weight:700;
      cursor:pointer;
      text-decoration:none;
      display:inline-flex;
      align-items:center;
      justify-content:center;
      gap:7px;
      white-space:nowrap;
    }
    button.secondary, a.button-link.secondary { background:#fff; color:var(--accent); }
    button.blue, a.button-link.blue { border-color:var(--blue); background:var(--blue); }
    .button-icon { width:18px; min-width:18px; text-align:center; line-height:1; }
    .status { min-height:24px; margin-top:10px; font-weight:700; }
    .status.error { color:var(--bad); }
    .scope-layout { display:grid; grid-template-columns:minmax(0,1.42fr) minmax(300px,.58fr); gap:14px; align-items:start; }
    .scope-progress { border:1px solid var(--line); border-radius:8px; background:#f8fafc; padding:12px; display:grid; gap:8px; }
    .scope-progress strong { font-size:20px; }
    .progress { height:10px; border-radius:999px; background:#e2e8f0; overflow:hidden; }
    .progress span { display:block; height:100%; background:var(--accent); border-radius:999px; }
    .progress.warn span { background:#f59e0b; }
    .progress.bad span { background:#ef4444; }
    .major-grid { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:8px; margin-top:12px; }
    .major-tile, .node {
      width:100%;
      min-height:58px;
      border:1px solid var(--line);
      border-radius:8px;
      background:#fff;
      color:var(--ink);
      text-align:left;
      padding:9px 10px;
      display:grid;
      gap:4px;
      cursor:pointer;
    }
    .major-tile { grid-template-columns:34px minmax(0,1fr); align-items:center; }
    .major-tile.active, .node.active { border-color:var(--accent); background:var(--accent-soft); box-shadow:0 0 0 2px rgba(15,118,110,.12); }
    .major-icon {
      width:30px;
      height:30px;
      border-radius:8px;
      display:flex;
      align-items:center;
      justify-content:center;
      background:#eef2ff;
      color:#1e3a8a;
      font-size:18px;
    }
    .tile-body { min-width:0; }
    .tile-title, .node-title { display:block; font-weight:700; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .tile-meta, .node-sub { display:block; color:var(--muted); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .taxonomy-columns { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-top:12px; }
    .taxonomy-column { border:1px solid var(--line); border-radius:8px; background:#fbfdff; padding:10px; min-height:184px; }
    .taxonomy-column header { display:flex; justify-content:space-between; gap:8px; align-items:center; margin-bottom:8px; }
    .taxonomy-list { display:grid; gap:6px; max-height:270px; overflow:auto; padding-right:2px; }
    .node { grid-template-columns:44px minmax(0,1fr); align-items:start; }
    .node-code { border:1px solid var(--line); border-radius:7px; background:#fff; color:#334155; font-weight:800; text-align:center; padding:3px 2px; font-size:12px; }
    .pipeline { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; margin:14px 0; }
    .stage { border:1px solid var(--line); border-radius:8px; background:#fff; padding:12px; display:grid; gap:6px; min-height:132px; }
    .stage-head { display:flex; gap:8px; align-items:center; }
    .stage-icon {
      width:32px;
      height:32px;
      border-radius:8px;
      display:flex;
      align-items:center;
      justify-content:center;
      background:#f1f5f9;
      font-size:18px;
    }
    .stage b { font-size:14px; }
    .stage strong { font-size:24px; line-height:1.1; }
    .stage.good .stage-icon { background:#dcfce7; }
    .stage.warn .stage-icon { background:#fef3c7; }
    .stage.bad .stage-icon { background:#fee2e2; }
    .cards { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:12px; background:#fbfdff; display:grid; gap:7px; min-height:118px; }
    .metric-head { display:flex; gap:8px; align-items:center; }
    .metric-icon {
      width:30px;
      height:30px;
      border-radius:8px;
      display:flex;
      align-items:center;
      justify-content:center;
      background:#eef2ff;
      color:#1e3a8a;
      font-size:17px;
    }
    .metric span.label { color:var(--muted); font-size:12px; font-weight:700; }
    .metric strong { font-size:24px; line-height:1.1; }
    .metric.good strong { color:var(--ok); }
    .metric.warn strong { color:var(--warn); }
    .metric.bad strong { color:var(--bad); }
    .pill { display:inline-flex; align-items:center; gap:5px; border:1px solid var(--line); border-radius:999px; padding:3px 8px; margin:0 4px 4px 0; background:#fff; font-size:12px; }
    .pill.good { border-color:#22c55e; color:#166534; background:#f0fdf4; }
    .pill.warn { border-color:#f59e0b; color:#92400e; background:#fffbeb; }
    .pill.bad { border-color:#ef4444; color:#991b1b; background:#fef2f2; }
    .next-grid { display:grid; grid-template-columns:minmax(0,1fr) minmax(280px,.42fr); gap:12px; align-items:start; }
    .quick-links { display:grid; gap:8px; }
    details { border:1px solid var(--line); border-radius:8px; background:#fbfdff; padding:10px 12px; }
    summary { cursor:pointer; font-weight:700; }
    .count-block { display:grid; gap:8px; margin-top:10px; }
    @media (max-width:1120px) {
      .pipeline { grid-template-columns:repeat(3,minmax(0,1fr)); }
      .cards, .scope-layout, .taxonomy-columns, .next-grid { grid-template-columns:1fr; }
      .major-grid { grid-template-columns:repeat(3,minmax(0,1fr)); }
    }
    @media (max-width:680px) {
      main { padding:12px; }
      .topbar { display:grid; }
      .top-actions { justify-content:flex-start; }
      .pipeline, .cards, .major-grid { grid-template-columns:1fr; }
      input.keyword { min-width:100%; }
      .field { width:100%; }
      button, a.button-link { width:100%; }
    }
  </style>
</head>
<body>
<main>
  <div class="topbar">
    <div>
      <h1>KSA 전처리 현황</h1>
      <p class="muted">휴먼 리뷰 화면과 분리한 읽기 전용 전처리 대시보드입니다. 원문 보존, 원자 KSA, 대표 개념, 짧은 라벨, 과업 근거의 준비 상태를 범위별로 보여줍니다.</p>
    </div>
    <div class="top-actions">
      <a class="button-link blue" href="/ksa-review-dashboard"><span class="button-icon" aria-hidden="true">✓</span>휴먼 리뷰 화면</a>
      <a class="button-link secondary" href="/"><span class="button-icon" aria-hidden="true">⌂</span>Main Dashboard</a>
    </div>
  </div>

  <section class="panel">
    <div class="toolbar">
      <div class="field"><label>대분류</label><input id="majorCode" class="code" value=""></div>
      <div class="field"><label>중분류</label><input id="middleCode" class="code" value=""></div>
      <div class="field"><label>소분류</label><input id="smallCode" class="code" value=""></div>
      <div class="field"><label>세분류</label><input id="subCode" class="code" value=""></div>
      <div class="field"><label>키워드</label><input id="keyword" class="keyword" placeholder="능력단위, KSA, 개념명"></div>
      <div class="field"><label>샘플 rows</label><input id="limit" class="code" value="1"></div>
      <button onclick="refreshPreprocessing()"><span class="button-icon" aria-hidden="true">🔎</span>조회</button>
      <button class="secondary" onclick="setHrScope()"><span class="button-icon" aria-hidden="true">🏢</span>인사 직무</button>
      <button class="secondary" onclick="clearFilters()"><span class="button-icon" aria-hidden="true">↺</span>전체</button>
    </div>
    <div id="status" class="status muted"></div>
  </section>

  <section class="panel">
    <div class="scope-layout">
      <div>
        <h2>분류별 전처리 탐색</h2>
        <div class="muted">대분류 아이콘과 중분류, 소분류, 세분류를 선택하면 아래 진행률이 같은 범위로 갱신됩니다.</div>
        <div id="preprocessMajorTiles" class="major-grid"></div>
      </div>
      <div id="scopeProgress" class="scope-progress">
        <span class="muted">분류를 선택하면 범위별 진행률이 표시됩니다.</span>
      </div>
    </div>
    <div class="taxonomy-columns">
      <section class="taxonomy-column">
        <header><strong>중분류</strong><span id="preprocessMiddleMeta" class="muted"></span></header>
        <div id="preprocessMiddleList" class="taxonomy-list"><span class="muted">대분류를 선택하세요.</span></div>
      </section>
      <section class="taxonomy-column">
        <header><strong>소분류</strong><span id="preprocessSmallMeta" class="muted"></span></header>
        <div id="preprocessSmallList" class="taxonomy-list"><span class="muted">중분류를 선택하세요.</span></div>
      </section>
      <section class="taxonomy-column">
        <header><strong>세분류</strong><span id="preprocessSubMeta" class="muted"></span></header>
        <div id="preprocessSubList" class="taxonomy-list"><span class="muted">소분류를 선택하세요.</span></div>
      </section>
    </div>
  </section>

  <section class="pipeline" id="pipeline"></section>

  <section class="panel" id="llmBacklogPanel">
    <h2>LLM Preprocessing Backlog</h2>
    <div class="cards" id="llmBacklogCards"></div>
    <div id="llmBacklogPolicy" class="muted"></div>
  </section>

  <section class="panel">
    <h2>핵심 진행률</h2>
    <div class="cards" id="cards"></div>
  </section>

  <section class="panel">
    <div class="next-grid">
      <div>
        <h2>다음 작업 판단</h2>
        <div id="nextSteps" class="muted"></div>
      </div>
      <div class="quick-links">
        <a class="button-link secondary" href="/ksa-review-dashboard?major_code=02&middle_code=02&small_code=02&sub_code=01"><span class="button-icon" aria-hidden="true">✓</span>인사 KSA 리뷰</a>
        <a class="button-link secondary" href="/ksa-label-needs-review-seedpack" target="_blank" rel="noopener"><span class="button-icon" aria-hidden="true">☑</span>라벨 리뷰팩</a>
        <a class="button-link secondary" href="/ksa-meaning-needs-review-seedpack" target="_blank" rel="noopener"><span class="button-icon" aria-hidden="true">☑</span>정의 리뷰팩</a>
      </div>
    </div>
  </section>

  <section class="panel">
    <details>
      <summary>상세 카운트 보기</summary>
      <div id="counts" class="count-block muted"></div>
    </details>
  </section>
</main>
<script>
const q = id => document.getElementById(id);
const fmt = new Intl.NumberFormat('ko-KR');
const majorIcons = {
  '01':'📊','02':'🏢','03':'💳','04':'🎓','05':'⚖','06':'🩺',
  '07':'🤝','08':'🎨','09':'🚚','10':'🛒','11':'🛡','12':'✈',
  '13':'🍽','14':'🏗','15':'⚙','16':'🧵','17':'🧪','18':'🛢',
  '19':'🔌','20':'💻','21':'📦','22':'🪵','23':'🌱','24':'🌾'
};
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function toNum(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? number : 0;
}
function pct(done, total) {
  return total ? done * 100 / total : 0;
}
function progressBar(percent, cls='') {
  const value = Math.max(0, Math.min(100, toNum(percent)));
  return `<div class="progress ${esc(cls)}"><span style="width:${value.toFixed(1)}%"></span></div>`;
}
function params() {
  const p = new URLSearchParams();
  for (const [id, key] of [
    ['majorCode', 'major_code'],
    ['middleCode', 'middle_code'],
    ['smallCode', 'small_code'],
    ['subCode', 'sub_code'],
    ['keyword', 'keyword'],
    ['limit', 'limit'],
  ]) {
    const value = q(id).value.trim();
    if (value) p.set(key, value);
  }
  return p;
}
function selectedCodes() {
  return {
    major: q('majorCode').value.trim(),
    middle: q('middleCode').value.trim(),
    small: q('smallCode').value.trim(),
    sub: q('subCode').value.trim(),
  };
}
function setScope(major='', middle='', small='', sub='') {
  q('majorCode').value = major || '';
  q('middleCode').value = middle || '';
  q('smallCode').value = small || '';
  q('subCode').value = sub || '';
}
function taxonomyParams(level) {
  const p = new URLSearchParams();
  const codes = selectedCodes();
  p.set('level', level);
  p.set('limit', level === 'major' ? '100' : '500');
  if (codes.major) p.set('major_code', codes.major);
  if (codes.middle) p.set('middle_code', codes.middle);
  if (codes.small) p.set('small_code', codes.small);
  if (codes.sub) p.set('sub_code', codes.sub);
  return p;
}
async function fetchJson(path) {
  const response = await fetch(path);
  const data = await response.json();
  if (!response.ok || data.ok === false) throw new Error((data.error && data.error.detail) || data.error || 'request failed');
  return data;
}
function nodeName(target) {
  const el = q(target)?.querySelector('.active .node-title, .active .tile-title');
  return el ? el.textContent : '';
}
function currentScopeLabel() {
  const names = [
    nodeName('preprocessMajorTiles'),
    nodeName('preprocessMiddleList'),
    nodeName('preprocessSmallList'),
    nodeName('preprocessSubList'),
  ].filter(Boolean);
  const codes = selectedCodes();
  const codeText = [codes.major, codes.middle, codes.small, codes.sub].filter(Boolean).join('-') || '전체 NCS';
  return names.length ? `${names.join(' > ')} (${codeText})` : codeText;
}
function renderMajorTiles(nodes) {
  const codes = selectedCodes();
  q('preprocessMajorTiles').innerHTML = (nodes || []).map(node => {
    const active = node.major_code === codes.major;
    const pctValue = toNum(node.element_percent);
    return `<button type="button" class="major-tile${active ? ' active' : ''}" onclick="selectPreprocessMajor('${esc(node.major_code)}')" aria-pressed="${active ? 'true' : 'false'}">
      <div class="major-icon" aria-hidden="true">${majorIcons[node.major_code] || esc(node.major_code)}</div>
      <div class="tile-body">
        <span class="tile-title">${esc(node.major_code)}. ${esc(node.name || '')}</span>
        <span class="tile-meta">API ${pctValue.toFixed(1)}% · ${fmt.format(toNum(node.element_matched))}/${fmt.format(toNum(node.element_count))}</span>
        ${progressBar(pctValue)}
      </div>
    </button>`;
  }).join('');
}
function renderNodeList(target, metaTarget, nodes, level) {
  const codes = selectedCodes();
  q(metaTarget).textContent = nodes && nodes.length ? `${fmt.format(nodes.length)}개` : '';
  if (!nodes || !nodes.length) {
    const messages = {middle:'대분류를 선택하세요.', small:'중분류를 선택하세요.', sub:'소분류를 선택하세요.'};
    q(target).innerHTML = `<span class="muted">${esc(messages[level] || '결과가 없습니다.')}</span>`;
    return;
  }
  q(target).innerHTML = nodes.map(node => {
    const active =
      (level === 'middle' && node.middle_code === codes.middle) ||
      (level === 'small' && node.small_code === codes.small) ||
      (level === 'sub' && node.sub_code === codes.sub);
    const pctValue = toNum(node.element_percent);
    const click =
      level === 'middle'
        ? `selectPreprocessMiddle('${esc(node.major_code)}','${esc(node.middle_code)}')`
        : level === 'small'
          ? `selectPreprocessSmall('${esc(node.major_code)}','${esc(node.middle_code)}','${esc(node.small_code)}')`
          : `selectPreprocessSub('${esc(node.major_code)}','${esc(node.middle_code)}','${esc(node.small_code)}','${esc(node.sub_code)}')`;
    return `<button type="button" class="node${active ? ' active' : ''}" onclick="${click}" aria-pressed="${active ? 'true' : 'false'}">
      <span class="node-code">${esc(node.code || '')}</span>
      <span>
        <span class="node-title">${esc(node.name || '')}</span>
        <span class="node-sub">API ${pctValue.toFixed(1)}% · 능력단위 ${fmt.format(toNum(node.unit_count))}</span>
        ${progressBar(pctValue)}
      </span>
    </button>`;
  }).join('');
}
async function loadPreprocessTaxonomy() {
  const codes = selectedCodes();
  const majors = await fetchJson('/api/taxonomy?' + taxonomyParams('major').toString());
  const middles = codes.major ? await fetchJson('/api/taxonomy?' + taxonomyParams('middle').toString()) : {nodes:[]};
  const smalls = codes.major && codes.middle ? await fetchJson('/api/taxonomy?' + taxonomyParams('small').toString()) : {nodes:[]};
  const subs = codes.major && codes.middle && codes.small ? await fetchJson('/api/taxonomy?' + taxonomyParams('sub').toString()) : {nodes:[]};
  renderMajorTiles(majors.nodes || []);
  renderNodeList('preprocessMiddleList', 'preprocessMiddleMeta', middles.nodes || [], 'middle');
  renderNodeList('preprocessSmallList', 'preprocessSmallMeta', smalls.nodes || [], 'small');
  renderNodeList('preprocessSubList', 'preprocessSubMeta', subs.nodes || [], 'sub');
}
async function selectPreprocessMajor(major) {
  setScope(major, '', '', '');
  await refreshPreprocessing();
}
async function selectPreprocessMiddle(major, middle) {
  setScope(major, middle, '', '');
  await refreshPreprocessing();
}
async function selectPreprocessSmall(major, middle, small) {
  setScope(major, middle, small, '');
  await refreshPreprocessing();
}
async function selectPreprocessSub(major, middle, small, sub) {
  setScope(major, middle, small, sub);
  await refreshPreprocessing();
}
function stage(icon, label, value, detail, cls='') {
  return `<div class="stage ${esc(cls)}">
    <div class="stage-head"><span class="stage-icon" aria-hidden="true">${icon}</span><b>${esc(label)}</b></div>
    <strong>${esc(value)}</strong>
    <span class="muted">${esc(detail || '')}</span>
  </div>`;
}
function metric(icon, label, value, detail, percent, cls='') {
  return `<div class="metric ${esc(cls)}">
    <div class="metric-head"><span class="metric-icon" aria-hidden="true">${icon}</span><span class="label">${esc(label)}</span></div>
    <strong>${esc(value)}</strong>
    <div class="muted">${esc(detail || '')}</div>
    ${percent === null ? '' : progressBar(percent, cls === 'bad' ? 'bad' : (cls === 'warn' ? 'warn' : ''))}
  </div>`;
}
function countMap(label, values) {
  const entries = Object.entries(values || {});
  if (!entries.length) return `<div><strong>${esc(label)}</strong> <span class="pill">none</span></div>`;
  return `<div><strong>${esc(label)}</strong> ${entries.map(([key, value]) => `<span class="pill">${esc(key)} ${fmt.format(toNum(value))}</span>`).join('')}</div>`;
}
function renderScopeProgress(data) {
  const progress = data.label_review_progress || {};
  const total = toNum(progress.total);
  const actioned = toNum(progress.actioned);
  const human = toNum(progress.human_reviewed);
  const pending = toNum(progress.pending);
  const needs = toNum(progress.needs_review);
  const actionedPercent = toNum(progress.actioned_percent);
  const humanPercent = toNum(progress.human_reviewed_percent);
  q('scopeProgress').innerHTML = `
    <span class="muted">현재 범위</span>
    <strong>${esc(currentScopeLabel())}</strong>
    <div><b>라벨 정리</b> ${fmt.format(actioned)} / ${fmt.format(total)} (${actionedPercent.toFixed(1)}%)</div>
    ${progressBar(actionedPercent)}
    <div><b>사람확인</b> ${fmt.format(human)} / ${fmt.format(total)} (${humanPercent.toFixed(1)}%)</div>
    ${progressBar(humanPercent, human ? '' : 'warn')}
    <div class="muted">대기 ${fmt.format(pending)} · 수정필요 ${fmt.format(needs)}</div>
  `;
}
function renderPreprocessing(data) {
  const s = data.summary || {};
  const labelProgress = data.label_review_progress || {};
  const matching = toNum(s.matching_ksa);
  const atomic = toNum(s.atomic_preprocessed_ksa);
  const linked = toNum(s.linked_ksa);
  const labelCandidates = toNum(s.label_candidate_concepts);
  const definitionCandidates = toNum(s.candidate_definition_concepts);
  const taskEvidence = toNum(s.criteria_evidence_linked_ksa) || toNum(s.task_context_evidence_concepts);
  const labelTotal = toNum(labelProgress.total);
  const actioned = toNum(labelProgress.actioned);
  const human = toNum(labelProgress.human_reviewed);
  const actionedPercent = toNum(labelProgress.actioned_percent);
  const humanPercent = toNum(labelProgress.human_reviewed_percent);
  renderScopeProgress(data);
  q('pipeline').innerHTML = [
    stage('📄', '1 원문 KSA', fmt.format(matching), '수정 금지 원천', matching ? 'good' : 'warn'),
    stage('✂', '2 원자 KSA', fmt.format(atomic), `${pct(atomic, matching).toFixed(1)}%`, atomic ? 'good' : 'warn'),
    stage('🔗', '3 대표 개념', fmt.format(linked), `${pct(linked, matching).toFixed(1)}%`, linked ? 'good' : 'warn'),
    stage('🏷', '4 짧은 라벨', fmt.format(labelCandidates), '휴먼 리뷰 후보', labelCandidates ? 'good' : 'warn'),
    stage('🧾', '5 정의 후보', fmt.format(definitionCandidates), '자동 승격 금지', definitionCandidates ? 'warn' : ''),
    stage('🧭', '6 과업 근거', fmt.format(taskEvidence), `${pct(taskEvidence, matching).toFixed(1)}%`, taskEvidence ? 'good' : 'warn'),
  ].join('');
  q('cards').innerHTML = [
    metric('✂', '원자 KSA 생성률', `${pct(atomic, matching).toFixed(1)}%`, `${fmt.format(atomic)} / ${fmt.format(matching)}`, pct(atomic, matching), atomic ? 'good' : 'warn'),
    metric('🔗', '대표 개념 연결률', `${pct(linked, matching).toFixed(1)}%`, `${fmt.format(linked)} / ${fmt.format(matching)}`, pct(linked, matching), linked ? 'good' : 'warn'),
    metric('🧭', '과업 근거 연결률', `${pct(taskEvidence, matching).toFixed(1)}%`, `${fmt.format(taskEvidence)} / ${fmt.format(matching)}`, pct(taskEvidence, matching), taskEvidence ? 'good' : 'warn'),
    metric('🏷', '라벨 정리 진행률', `${actionedPercent.toFixed(1)}%`, `${fmt.format(actioned)} / ${fmt.format(labelTotal)}`, actionedPercent, actioned ? 'good' : 'warn'),
    metric('✓', '사람확인 진행률', `${humanPercent.toFixed(1)}%`, `${fmt.format(human)} / ${fmt.format(labelTotal)}`, humanPercent, human ? 'good' : 'warn'),
    metric('📌', '조회 기준', labelProgress.unit_label || 'Filtered KSA rows', `샘플 ${fmt.format((data.items || []).length)} rows`, null),
  ].join('');
  const blockers = [];
  if (toNum(s.unlinked_ksa)) blockers.push(['bad', '대표 개념 미연결', toNum(s.unlinked_ksa)]);
  if (toNum(labelProgress.pending)) blockers.push(['warn', '라벨 대기', toNum(labelProgress.pending)]);
  if (toNum(labelProgress.needs_review)) blockers.push(['bad', '라벨 수정필요', toNum(labelProgress.needs_review)]);
  if (toNum(s.generic_label_candidate_concepts)) blockers.push(['warn', '범용 라벨 후보', toNum(s.generic_label_candidate_concepts)]);
  if (toNum(s.provenance_missing_label_candidate_concepts)) blockers.push(['bad', '출처 누락 라벨', toNum(s.provenance_missing_label_candidate_concepts)]);
  q('nextSteps').innerHTML = blockers.length
    ? blockers.map(([cls, label, value]) => `<span class="pill ${esc(cls)}">${esc(label)} ${fmt.format(value)}</span>`).join('')
    : '<span class="pill good">현재 조회 범위에서 큰 전처리 경고가 없습니다.</span>';
  q('counts').innerHTML = [
    countMap('definition_status', data.definition_status_counts),
    countMap('concept_review_status', data.concept_review_status_counts),
    countMap('label_review_status', data.label_review_status_counts),
    countMap('meaning_review_status', data.meaning_review_status_counts),
    countMap('label_quality_flags', data.label_quality_flag_counts),
  ].join('');
  q('status').textContent = `${fmt.format(matching)}개 KSA 기준 전처리 현황을 불러왔습니다.`;
}
function renderLlmBacklog(status) {
  const llm = status.llm_preprocessing_backlog || {};
  const plan = status.llm_preprocessing_work_plan || {};
  if (!llm.available) {
    q('llmBacklogCards').innerHTML = [
      metric('AI', 'Backlog map', 'not available', 'Run llm-preprocessing-backlog-map to generate it.', null, 'warn')
    ].join('');
    q('llmBacklogPolicy').innerHTML = plan.available
      ? `<div class="count-block"><strong>Work plan</strong> <span class="pill ${plan.safety_ok ? 'good' : 'bad'}">${esc(plan.next_action || plan.status || '')}</span></div>`
      : '';
    return;
  }
  const safetyClass = llm.safety_ok ? 'good' : 'bad';
  q('llmBacklogCards').innerHTML = [
    metric('AI', 'Label candidates', fmt.format(toNum(llm.label_candidate_rows)), `${fmt.format(toNum(llm.pending_label_rows_not_trusted))} not trusted`, null, llm.safety_ok ? 'good' : 'warn'),
    metric('OK', 'Human-reviewed labels', fmt.format(toNum(llm.human_reviewed_label_rows)), `${fmt.format(toNum(llm.ontology_concepts_human_reviewed))} human-reviewed concepts`, null, toNum(llm.human_reviewed_label_rows) ? 'warn' : 'bad'),
    metric('DEF', 'Meaning candidates', fmt.format(toNum(llm.meaning_candidate_rows)), `${fmt.format(toNum(llm.ontology_concepts))} ontology concepts`, null, 'warn'),
    metric('SMP', 'Recommended samples', fmt.format(toNum(llm.recommended_sample_rows_total)), `${esc(llm.estimated_click_reduction_ratio ?? '')} click reduction ratio`, null, 'good'),
    metric('LNK', 'Task/course links', fmt.format(toNum(llm.task_ksa_concept_relation_rows)), `${fmt.format(toNum(llm.training_goal_concept_link_rows))} training goal links`, null, 'warn'),
    metric('POL', 'Safety', llm.safety_ok ? 'safe' : 'blocked', `${fmt.format(toNum(llm.source_issue_count))} source issues`, null, safetyClass),
  ].join('');
  const statuses = (llm.non_approval_statuses || []).map(value => `<span class="pill warn">${esc(value)}</span>`).join('');
  const classes = llm.auto_triage_classification_counts || {};
  const tracks = (plan.tracks || []).map(row => `<span class="pill ${plan.safety_ok ? 'good' : 'warn'}">${esc(row.priority || '')} ${esc(row.track || '')}: ${fmt.format(toNum(row.input_rows))}</span>`).join('');
  q('llmBacklogPolicy').innerHTML = `
    <div class="count-block">
      <div><strong>Non-approval statuses</strong> ${statuses || '<span class="pill">none</span>'}</div>
      ${countMap('auto-triage classes', classes)}
      <div><strong>Work plan</strong> <span class="pill ${plan.safety_ok ? 'good' : 'bad'}">${esc(plan.next_action || (plan.available ? plan.status : 'not available'))}</span> ${tracks}</div>
      <div><strong>source</strong> <span class="pill">${esc(llm.path || '')}</span></div>
    </div>`;
}
async function loadReviewStatus() {
  try {
    const status = await fetchJson('/api/ksa-review-status');
    renderLlmBacklog(status);
  } catch (err) {
    q('llmBacklogCards').innerHTML = [
      metric('AI', 'Backlog map', 'unavailable', err.message, null, 'warn')
    ].join('');
    q('llmBacklogPolicy').textContent = '';
  }
}
function applyInitialQueryParams() {
  const search = new URLSearchParams(window.location.search);
  for (const [id, key] of [
    ['majorCode', 'major_code'],
    ['middleCode', 'middle_code'],
    ['smallCode', 'small_code'],
    ['subCode', 'sub_code'],
    ['keyword', 'keyword'],
    ['limit', 'limit'],
  ]) {
    if (search.has(key)) q(id).value = search.get(key) || '';
  }
}
async function loadPreprocessing() {
  q('status').className = 'status muted';
  q('status').textContent = 'Loading...';
  try {
    const data = await fetchJson('/api/ksa-definitions?' + params().toString());
    renderPreprocessing(data);
  } catch (err) {
    q('status').className = 'status error';
    q('status').textContent = err.message;
  }
}
async function refreshPreprocessing() {
  q('status').className = 'status muted';
  q('status').textContent = 'Loading...';
  try {
    await loadPreprocessTaxonomy();
    await loadPreprocessing();
    await loadReviewStatus();
  } catch (err) {
    q('status').className = 'status error';
    q('status').textContent = err.message;
  }
}
async function setHrScope() {
  setScope('02', '02', '02', '01');
  await refreshPreprocessing();
}
async function clearFilters() {
  for (const id of ['majorCode', 'middleCode', 'smallCode', 'subCode', 'keyword']) q(id).value = '';
  q('limit').value = '1';
  await refreshPreprocessing();
}
applyInitialQueryParams();
refreshPreprocessing();
</script>
</body>
</html>
"""


DASHBOARD_STATUS_COUNT_TABLES = (
    "raw_excel_rows",
    "classifications",
    "competency_units",
    "competency_elements",
    "performance_criteria",
    "ksa_items",
    "api_raw_responses",
    "api_competency_units",
    "sqf_duties",
    "quality_issues",
)


def dashboard_status_error_payload(exc: DashboardReadOnlyError) -> dict:
    return {
        "ok": False,
        "error": exc.to_payload(),
        "generated_at": now_utc(),
        "counts": {table: 0 for table in DASHBOARD_STATUS_COUNT_TABLES},
        "unit_api_status": {},
        "element_api_status": {},
        "element_progress": {
            "total": 0,
            "matched": 0,
            "not_collected": 0,
            "api_failed": 0,
            "no_data": 0,
            "percent": 0,
        },
        "quality": {
            "open_issues": 0,
            "resolved_issues": 0,
            "open_by_severity": {},
            "info_issues": 0,
            "actionable_issues": 0,
            "human_review_required_issues": 0,
            "api_issues": 0,
        },
        "sqf": {
            "major_codes_with_data": 0,
            "management_support_duties": 0,
            "duties_with_training": 0,
        },
        "ontology": {
            "match_table_present": False,
            "matches": 0,
            "reviewed_matches": 0,
        },
        "issue_types": [],
        "missing_duty_definitions": 0,
    }


def get_status(db_path: Path) -> dict:
    try:
        conn = connect_db_for_read(db_path, required_tables=DASHBOARD_STATUS_COUNT_TABLES)
    except DashboardReadOnlyError as exc:
        return dashboard_status_error_payload(exc)
    counts = {
        table: scalar(conn, f"SELECT COUNT(*) FROM {table}")
        for table in DASHBOARD_STATUS_COUNT_TABLES
    }
    match_table_present = table_exists(conn, "sqf_ncs_matches")
    match_count = scalar(conn, "SELECT COUNT(*) FROM sqf_ncs_matches") if match_table_present else 0
    reviewed_match_count = (
        scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM sqf_ncs_matches
            WHERE review_status IN ('reviewed', 'human_reviewed', 'accepted')
            """,
        )
        if match_table_present
        else 0
    )
    sqf_major_codes = scalar(
        conn,
        "SELECT COUNT(DISTINCT ncs_lclas_cd) FROM sqf_duties",
    )
    sqf_management_support = scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM sqf_duties
        WHERE ncs_lclas_cd = '02'
          AND sqf_field_name = '경영관리'
          AND job_name = '경영지원'
        """,
    )
    sqf_with_education = scalar(
        conn,
        """
        SELECT COUNT(*)
        FROM sqf_duties
        WHERE duty_education_training IS NOT NULL
          AND TRIM(duty_education_training) <> ''
          AND TRIM(duty_education_training) <> '-'
        """,
    )
    element_status = {
        row["api_match_status"]: row["count"]
        for row in conn.execute(
            "SELECT api_match_status, COUNT(*) AS count FROM competency_elements GROUP BY api_match_status"
        )
    }
    unit_status = {
        row["api_match_status"]: row["count"]
        for row in conn.execute(
            "SELECT api_match_status, COUNT(*) AS count FROM competency_units GROUP BY api_match_status"
        )
    }
    total_elements = counts["competency_elements"]
    matched = int(element_status.get("matched", 0))
    issue_types = [
        row["issue_type"]
        for row in conn.execute(
            """
            SELECT issue_type
            FROM quality_issues
            WHERE resolved_at IS NULL
            GROUP BY issue_type
            ORDER BY issue_type
            """
        )
    ]
    open_quality_by_severity = {
        row["severity"]: row["count"]
        for row in conn.execute(
            """
            SELECT severity, COUNT(*) AS count
            FROM quality_issues
            WHERE resolved_at IS NULL
            GROUP BY severity
            """
        )
    }
    info_issues = int(open_quality_by_severity.get("info", 0))
    open_issues = scalar(conn, "SELECT COUNT(*) FROM quality_issues WHERE resolved_at IS NULL")
    resolved_issues = scalar(conn, "SELECT COUNT(*) FROM quality_issues WHERE resolved_at IS NOT NULL")
    actionable_issues = max(0, int(open_issues) - info_issues)
    payload = {
        "generated_at": now_utc(),
        "counts": counts,
        "unit_api_status": unit_status,
        "element_api_status": element_status,
        "element_progress": {
            "total": total_elements,
            "matched": matched,
            "not_collected": int(element_status.get("not_collected", 0)),
            "api_failed": int(element_status.get("api_failed", 0)),
            "no_data": int(element_status.get("no_data", 0)),
            "percent": (matched / total_elements * 100) if total_elements else 0,
        },
        "quality": {
            "open_issues": open_issues,
            "resolved_issues": resolved_issues,
            "open_by_severity": open_quality_by_severity,
            "info_issues": info_issues,
            "actionable_issues": actionable_issues,
            "human_review_required_issues": scalar(
                conn,
                """
                SELECT COUNT(*)
                FROM quality_issues
                WHERE resolved_at IS NULL
                  AND issue_type LIKE '%human_review_required%'
                """,
            ),
            "api_issues": scalar(
                conn,
                """
                SELECT COUNT(*)
                FROM quality_issues
                WHERE resolved_at IS NULL
                  AND issue_type LIKE 'api%'
                """,
            ),
        },
        "sqf": {
            "major_codes_with_data": sqf_major_codes,
            "management_support_duties": sqf_management_support,
            "duties_with_training": sqf_with_education,
        },
        "ontology": {
            "match_table_present": match_table_present,
            "matches": match_count,
            "reviewed_matches": reviewed_match_count,
        },
        "issue_types": issue_types,
        "missing_duty_definitions": scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM classifications
            WHERE duty_def_api IS NULL OR TRIM(duty_def_api) = ''
            """,
        ),
    }
    conn.close()
    return payload


def count_query(conn, sql: str, values: list[str]) -> int:
    return scalar(conn, sql, values)


def percent(completed: int, total: int) -> float:
    return (completed / total * 100) if total else 100.0


def quality_issue_scope_filter(params: dict[str, list[str]]) -> tuple[str, list[str]]:
    scope_clauses, scope_values = classification_filters(params, "c")
    if not scope_clauses:
        return "", []
    scope_sql = " AND ".join(scope_clauses)
    clause = f"""
    (
      (
        qi.target_type = 'classification'
        AND EXISTS (
          SELECT 1
          FROM classifications c
          WHERE c.classification_id = CAST(qi.target_id AS INTEGER)
            AND {scope_sql}
        )
      )
      OR (
        qi.target_type = 'unit'
        AND EXISTS (
          SELECT 1
          FROM competency_units cu
          JOIN classifications c ON c.classification_id = cu.classification_id
          WHERE cu.unit_code = qi.target_id
            AND {scope_sql}
        )
      )
      OR (
        qi.target_type = 'element'
        AND EXISTS (
          SELECT 1
          FROM competency_elements ce
          JOIN competency_units cu ON cu.unit_code = ce.unit_code
          JOIN classifications c ON c.classification_id = cu.classification_id
          WHERE ce.element_id = CAST(qi.target_id AS INTEGER)
            AND {scope_sql}
        )
      )
      OR (
        qi.target_type = 'criteria'
        AND EXISTS (
          SELECT 1
          FROM performance_criteria pc
          JOIN competency_elements ce ON ce.element_id = pc.element_id
          JOIN competency_units cu ON cu.unit_code = ce.unit_code
          JOIN classifications c ON c.classification_id = cu.classification_id
          WHERE pc.criteria_id = CAST(qi.target_id AS INTEGER)
            AND {scope_sql}
        )
      )
      OR (
        qi.target_type = 'ksa'
        AND EXISTS (
          SELECT 1
          FROM ksa_items ki
          JOIN competency_elements ce ON ce.element_id = ki.element_id
          JOIN competency_units cu ON cu.unit_code = ce.unit_code
          JOIN classifications c ON c.classification_id = cu.classification_id
          WHERE ki.ksa_id = CAST(qi.target_id AS INTEGER)
            AND {scope_sql}
        )
      )
      OR (
        qi.target_type = 'ontology_concept'
        AND (
          EXISTS (
            SELECT 1
            FROM ksa_concept_links kcl
            JOIN ksa_items ki ON ki.ksa_id = kcl.ksa_id
            JOIN competency_elements ce ON ce.element_id = ki.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE kcl.concept_id = CAST(qi.target_id AS INTEGER)
              AND {scope_sql}
          )
          OR EXISTS (
            SELECT 1
            FROM criteria_concept_links ccl
            JOIN performance_criteria pc ON pc.criteria_id = ccl.criteria_id
            JOIN competency_elements ce ON ce.element_id = pc.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE ccl.concept_id = CAST(qi.target_id AS INTEGER)
              AND {scope_sql}
          )
          OR EXISTS (
            SELECT 1
            FROM training_goal_concept_links tgcl
            LEFT JOIN competency_elements ce ON ce.element_id = tgcl.element_id
            LEFT JOIN competency_units element_unit ON element_unit.unit_code = ce.unit_code
            LEFT JOIN competency_units direct_unit ON direct_unit.unit_code = tgcl.unit_code
            JOIN classifications c
              ON c.classification_id = COALESCE(element_unit.classification_id, direct_unit.classification_id)
            WHERE tgcl.concept_id = CAST(qi.target_id AS INTEGER)
              AND {scope_sql}
          )
        )
      )
      OR (
        qi.target_type = 'task_ksa_concept_relation'
        AND EXISTS (
          SELECT 1
          FROM task_ksa_concept_relations tkcr
          JOIN competency_elements ce ON ce.element_id = tkcr.element_id
          JOIN competency_units cu ON cu.unit_code = ce.unit_code
          JOIN classifications c ON c.classification_id = cu.classification_id
          WHERE tkcr.relation_id = CAST(qi.target_id AS INTEGER)
            AND {scope_sql}
        )
      )
      OR (
        qi.target_type = 'training_goal_concept_link'
        AND EXISTS (
          SELECT 1
          FROM training_goal_concept_links tgcl
          LEFT JOIN competency_elements ce ON ce.element_id = tgcl.element_id
          LEFT JOIN competency_units element_unit ON element_unit.unit_code = ce.unit_code
          LEFT JOIN competency_units direct_unit ON direct_unit.unit_code = tgcl.unit_code
          JOIN classifications c
            ON c.classification_id = COALESCE(element_unit.classification_id, direct_unit.classification_id)
          WHERE tgcl.link_id = CAST(qi.target_id AS INTEGER)
            AND {scope_sql}
        )
      )
    )
    """
    values: list[str] = []
    for _ in range(10):
        values.extend(scope_values)
    return clause, values


def phase(
    *,
    name: str,
    meaning: str,
    completed: int,
    total: int,
    remaining: int,
    remaining_detail: str,
    method: str,
    kind: str,
    state: str,
    title: str,
) -> dict:
    return {
        "name": name,
        "meaning": meaning,
        "completed": completed,
        "total": total,
        "remaining": remaining,
        "remaining_detail": remaining_detail,
        "percent": percent(completed, total),
        "status": "complete" if remaining == 0 else "in_progress",
        "method": method,
        "kind": kind,
        "state": state,
        "title": title,
    }


def get_progress(db_path: Path, params: dict[str, list[str]]) -> dict:
    conn = connect_db_for_read(db_path, required_tables=DASHBOARD_LOOKUP_TABLES)
    where_c, vals_c = scoped_where(params, "c")
    where_r, vals_r = scoped_where(params, "r")

    raw_rows = count_query(conn, f"SELECT COUNT(*) FROM raw_excel_rows r {where_r}", vals_r)
    classifications = count_query(conn, f"SELECT COUNT(*) FROM classifications c {where_c}", vals_c)
    duty_defs = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM classifications c
        {where_c} {"AND" if where_c else "WHERE"} c.duty_def_api IS NOT NULL AND TRIM(c.duty_def_api) <> ''
        """,
        vals_c,
    )
    units = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c}
        """,
        vals_c,
    )
    units_matched = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c} {"AND" if where_c else "WHERE"} cu.api_match_status = 'matched'
        """,
        vals_c,
    )
    elements = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM competency_elements ce
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c}
        """,
        vals_c,
    )
    elements_matched = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM competency_elements ce
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c} {"AND" if where_c else "WHERE"} ce.api_match_status = 'matched'
        """,
        vals_c,
    )
    elements_not_collected = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM competency_elements ce
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c} {"AND" if where_c else "WHERE"} ce.api_match_status = 'not_collected'
        """,
        vals_c,
    )
    elements_problem = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM competency_elements ce
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c} {"AND" if where_c else "WHERE"} ce.api_match_status IN ('api_failed', 'no_data')
        """,
        vals_c,
    )
    criteria = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM performance_criteria pc
        JOIN competency_elements ce ON ce.element_id = pc.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c}
        """,
        vals_c,
    )
    criteria_refined = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM performance_criteria pc
        JOIN competency_elements ce ON ce.element_id = pc.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c} {"AND" if where_c else "WHERE"} pc.criteria_text_refined IS NOT NULL AND TRIM(pc.criteria_text_refined) <> ''
        """,
        vals_c,
    )
    ksa = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM ksa_items ki
        JOIN competency_elements ce ON ce.element_id = ki.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c}
        """,
        vals_c,
    )
    ksa_refined = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM ksa_items ki
        JOIN competency_elements ce ON ce.element_id = ki.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where_c} {"AND" if where_c else "WHERE"} ki.ksa_text_refined IS NOT NULL AND TRIM(ki.ksa_text_refined) <> ''
        """,
        vals_c,
    )
    issue_scope, issue_scope_values = quality_issue_scope_filter(params)
    open_issue_where = "WHERE qi.resolved_at IS NULL"
    resolved_issue_where = "WHERE qi.resolved_at IS NOT NULL"
    if issue_scope:
        open_issue_where += f" AND {issue_scope}"
        resolved_issue_where += f" AND {issue_scope}"
    open_issues = count_query(
        conn,
        f"SELECT COUNT(*) FROM quality_issues qi {open_issue_where}",
        issue_scope_values,
    )
    resolved_issues = count_query(
        conn,
        f"SELECT COUNT(*) FROM quality_issues qi {resolved_issue_where}",
        issue_scope_values,
    )
    all_issues = open_issues + resolved_issues
    sqf_total = count_query(conn, "SELECT COUNT(*) FROM sqf_duties", [])
    sqf_major_codes = count_query(conn, "SELECT COUNT(DISTINCT ncs_lclas_cd) FROM sqf_duties", [])
    sqf_mvp = count_query(
        conn,
        """
        SELECT COUNT(*)
        FROM sqf_duties
        WHERE ncs_lclas_cd = '02'
          AND sqf_field_name = '경영관리'
          AND job_name = '경영지원'
        """,
        [],
    )
    sqf_mvp_with_definition = count_query(
        conn,
        """
        SELECT COUNT(*)
        FROM sqf_duties
        WHERE ncs_lclas_cd = '02'
          AND sqf_field_name = '경영관리'
          AND job_name = '경영지원'
          AND duty_definition IS NOT NULL
          AND TRIM(duty_definition) <> ''
        """,
        [],
    )
    match_table_present = table_exists(conn, "sqf_ncs_matches")
    sqf_matches = count_query(conn, "SELECT COUNT(*) FROM sqf_ncs_matches", []) if match_table_present else 0
    sqf_reviewed_matches = (
        count_query(
            conn,
            """
            SELECT COUNT(*)
            FROM sqf_ncs_matches
            WHERE review_status IN ('reviewed', 'human_reviewed', 'accepted')
            """,
            [],
        )
        if match_table_present
        else 0
    )

    phases = [
        phase(
            name="Excel 계층 정규화",
            meaning="플랫 Excel 행을 분류, 능력단위, 요소, 수행준거, KSA 테이블로 분리",
            completed=raw_rows,
            total=raw_rows,
            remaining=0 if raw_rows else 1,
            remaining_detail="원천 행 적재 완료" if raw_rows else "원천 Excel 적재 필요",
            method="중복 제거 + 계층 키 생성 + raw 원문 보존",
            kind="classification",
            state="processed",
            title="분류 전처리 완료",
        ),
        phase(
            name="능력단위 API 매칭",
            meaning="NCS005 기준정보와 Excel 능력단위를 능력단위 코드로 조인",
            completed=units_matched,
            total=units,
            remaining=max(units - units_matched, 0),
            remaining_detail="미매칭 능력단위 확인",
            method="NCS_CL_CD / unit_code 코드 매칭, API 정의 저장",
            kind="unit",
            state="api_matched" if units_matched else "processed",
            title="능력단위 API matched",
        ),
        phase(
            name="직무정의 API 보강",
            meaning="세분류/직무 정의를 API에서 받아 분류 테이블에 보강",
            completed=duty_defs,
            total=classifications,
            remaining=max(classifications - duty_defs, 0),
            remaining_detail="직무정의 누락 분류 확인",
            method="NCS004 DUTY_DEF 저장",
            kind="classification",
            state="processed",
            title="분류 전처리 완료",
        ),
        phase(
            name="능력단위요소 API 검증",
            meaning="Excel 요소가 NCS006 기준정보와 일치하는지 검증",
            completed=elements_matched,
            total=elements,
            remaining=max(elements - elements_matched, 0),
            remaining_detail=f"not_collected {elements_not_collected:,}, failed/no_data {elements_problem:,}",
            method="요소 번호 단위 API 조회, matched/api_failed/no_data 상태 저장",
            kind="element",
            state="api_not_collected" if elements_not_collected else "api_problem",
            title="요소 API 미수집" if elements_not_collected else "요소 API 실패/없음",
        ),
        phase(
            name="SQF 직무수준 수집",
            meaning="SQF openapi26 산업별 직무와 직무수준을 NCS 대분류 코드로 적재",
            completed=sqf_major_codes,
            total=24,
            remaining=max(24 - sqf_major_codes, 0),
            remaining_detail=f"제공 대분류 {sqf_major_codes:,}개, SQF 직무수준 {sqf_total:,}건",
            method="NCS_SQF_SERVICE_KEY + /openapi26, code 000 정상, 002 빈 데이터",
            kind="sqf",
            state="all",
            title="SQF 직무수준 전체",
        ),
        phase(
            name="경영지원 MVP 범위",
            meaning="1차 MVP를 SQF 02 > 경영관리 > 경영지원 직무로 제한",
            completed=sqf_mvp_with_definition,
            total=sqf_mvp,
            remaining=max(sqf_mvp - sqf_mvp_with_definition, 0),
            remaining_detail=f"경영지원 SQF 직무수준 {sqf_mvp:,}건",
            method="SQF job_name='경영지원'을 NCS 02 경영·회계·사무와 연결",
            kind="sqf",
            state="mvp",
            title="경영지원 MVP SQF 직무",
        ),
        phase(
            name="NCS-SQF 매핑 객체",
            meaning="SQF 직무수준과 NCS 능력단위/KSA 사이의 관계, 점수, 근거, 버전 저장",
            completed=sqf_reviewed_matches,
            total=max(sqf_matches, 1),
            remaining=(max(sqf_matches - sqf_reviewed_matches, 0) if match_table_present else 1),
            remaining_detail=(
                f"후보 {sqf_matches:,}건, 검토 {sqf_reviewed_matches:,}건"
                if match_table_present
                else "sqf_ncs_matches 테이블 생성 필요"
            ),
            method="sameAs 금지, requires/closeMatch/partiallyCovers + evidence/confidence 저장",
            kind="sqf",
            state="mvp",
            title="경영지원 MVP SQF 직무",
        ),
        phase(
            name="사람 수작업 정제",
            meaning="수행준거와 KSA의 정제본을 원문과 별도 저장",
            completed=criteria_refined + ksa_refined,
            total=criteria + ksa,
            remaining=max((criteria + ksa) - (criteria_refined + ksa_refined), 0),
            remaining_detail=f"수행준거 미정제 {criteria - criteria_refined:,}, KSA 미정제 {ksa - ksa_refined:,}",
            method="raw 필드 보존, refined 필드와 review_status만 갱신",
            kind="criteria",
            state="raw",
            title="수행준거 미정제",
        ),
        phase(
            name="품질 이슈 검토",
            meaning="중복, 누락, 짧은 문장 등 품질 진단 결과 처리",
            completed=resolved_issues,
            total=all_issues,
            remaining=open_issues,
            remaining_detail="열린 이슈를 상세 확인하거나 해결 처리",
            method="quality_issues 테이블 기반 Human-in-the-loop 검토",
            kind="quality",
            state="open",
            title="열린 품질 이슈",
        ),
    ]
    conn.close()
    return {"phases": phases}


def get_workbench(db_path: Path, params: dict[str, list[str]]) -> dict:
    conn = connect_db_for_read(db_path, required_tables=DASHBOARD_LOOKUP_TABLES)
    where_c, vals_c = scoped_where(params, "c")
    issue_scope, issue_scope_values = quality_issue_scope_filter(params)
    issue_where = "WHERE qi.resolved_at IS NULL"
    if issue_scope:
        issue_where += f" AND {issue_scope}"
    cards = [
        {
            "group": "DB 전처리 완료",
            "title": "분류 전처리 완료",
            "kind": "classification",
            "state": "processed",
            "count": count_query(conn, f"SELECT COUNT(*) FROM classifications c {where_c}", vals_c),
            "description": "정규화된 세분류/직무 항목",
        },
        {
            "group": "DB 전처리 완료",
            "title": "능력단위 전처리 완료",
            "kind": "unit",
            "state": "processed",
            "count": count_query(
                conn,
                f"""
                SELECT COUNT(*)
                FROM competency_units cu
                JOIN classifications c ON c.classification_id = cu.classification_id
                {where_c}
                """,
                vals_c,
            ),
            "description": "Excel에서 정규화된 능력단위",
        },
        {
            "group": "DB 전처리 완료",
            "title": "능력단위요소 전처리 완료",
            "kind": "element",
            "state": "processed",
            "count": count_query(
                conn,
                f"""
                SELECT COUNT(*)
                FROM competency_elements ce
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                {where_c}
                """,
                vals_c,
            ),
            "description": "Excel에서 정규화된 요소",
        },
        {
            "group": "수작업 전처리 필요",
            "title": "수행준거 미정제",
            "kind": "criteria",
            "state": "raw",
            "count": count_query(
                conn,
                f"""
                SELECT COUNT(*)
                FROM performance_criteria pc
                JOIN competency_elements ce ON ce.element_id = pc.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                {where_c} {"AND" if where_c else "WHERE"} (pc.criteria_text_refined IS NULL OR TRIM(pc.criteria_text_refined) = '')
                """,
                vals_c,
            ),
            "description": "사람이 정제본을 입력할 수 있는 수행준거",
        },
        {
            "group": "수작업 전처리 필요",
            "title": "KSA 미정제",
            "kind": "ksa",
            "state": "raw",
            "count": count_query(
                conn,
                f"""
                SELECT COUNT(*)
                FROM ksa_items ki
                JOIN competency_elements ce ON ce.element_id = ki.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                {where_c} {"AND" if where_c else "WHERE"} (ki.ksa_text_refined IS NULL OR TRIM(ki.ksa_text_refined) = '')
                """,
                vals_c,
            ),
            "description": "사람이 정제본을 입력할 수 있는 KSA",
        },
        {
            "group": "API 매칭 완료",
            "title": "능력단위 API matched",
            "kind": "unit",
            "state": "api_matched",
            "count": count_query(
                conn,
                f"""
                SELECT COUNT(*)
                FROM competency_units cu
                JOIN classifications c ON c.classification_id = cu.classification_id
                {where_c} {"AND" if where_c else "WHERE"} cu.api_match_status = 'matched'
                """,
                vals_c,
            ),
            "description": "NCS005/API 정의가 연결된 능력단위",
        },
        {
            "group": "API 매칭 완료",
            "title": "요소 API matched",
            "kind": "element",
            "state": "api_matched",
            "count": count_query(
                conn,
                f"""
                SELECT COUNT(*)
                FROM competency_elements ce
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                {where_c} {"AND" if where_c else "WHERE"} ce.api_match_status = 'matched'
                """,
                vals_c,
            ),
            "description": "NCS006/API 요소명이 검증된 요소",
        },
        {
            "group": "API 미처리",
            "title": "요소 API 미수집",
            "kind": "element",
            "state": "api_not_collected",
            "count": count_query(
                conn,
                f"""
                SELECT COUNT(*)
                FROM competency_elements ce
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                {where_c} {"AND" if where_c else "WHERE"} ce.api_match_status = 'not_collected'
                """,
                vals_c,
            ),
            "description": "아직 API 검증을 돌리지 않은 요소",
        },
        {
            "group": "API 미처리",
            "title": "요소 API 실패/없음",
            "kind": "element",
            "state": "api_problem",
            "count": count_query(
                conn,
                f"""
                SELECT COUNT(*)
                FROM competency_elements ce
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                {where_c} {"AND" if where_c else "WHERE"} ce.api_match_status IN ('api_failed', 'no_data')
                """,
                vals_c,
            ),
            "description": "사람이 원문 확인하거나 재수집 후보로 볼 요소",
        },
        {
            "group": "NCS-SQF 온톨로지",
            "title": "SQF 직무수준 전체",
            "kind": "sqf",
            "state": "all",
            "count": count_query(conn, "SELECT COUNT(*) FROM sqf_duties", []),
            "description": "openapi26에서 수집한 SQF 산업별 직무수준",
        },
        {
            "group": "NCS-SQF 온톨로지",
            "title": "경영지원 MVP SQF 직무",
            "kind": "sqf",
            "state": "mvp",
            "count": count_query(
                conn,
                """
                SELECT COUNT(*)
                FROM sqf_duties
                WHERE ncs_lclas_cd = '02'
                  AND sqf_field_name = '경영관리'
                  AND job_name = '경영지원'
                """,
                [],
            ),
            "description": "1차 MVP 범위: 02 경영관리 > 경영지원",
        },
        {
            "group": "NCS-SQF 온톨로지",
            "title": "직접 교육훈련 근거 있음",
            "kind": "sqf",
            "state": "training",
            "count": count_query(
                conn,
                """
                SELECT COUNT(*)
                FROM sqf_duties
                WHERE duty_education_training IS NOT NULL
                  AND TRIM(duty_education_training) <> ''
                  AND TRIM(duty_education_training) <> '-'
                """,
                [],
            ),
            "description": "SQF dutyEduTrain이 직접 채워진 직무수준",
        },
        {
            "group": "품질 검토",
            "title": "열린 품질 이슈",
            "kind": "quality",
            "state": "open",
            "count": count_query(
                conn,
                f"SELECT COUNT(*) FROM quality_issues qi {issue_where}",
                issue_scope_values,
            ),
            "description": "품질 진단에서 발견된 검토 항목",
        },
    ]
    conn.close()
    return {"cards": cards}


def get_taxonomy(db_path: Path, params: dict[str, list[str]]) -> dict:
    level = first(params, "level", "major")
    levels = {
        "major": {
            "code": "c.major_code",
            "name": "c.major_name",
            "select": [
                "c.major_code AS major_code",
                "'' AS middle_code",
                "'' AS small_code",
                "'' AS sub_code",
            ],
            "filters": [],
            "order": "c.major_code",
            "group": "c.major_code, c.major_name",
        },
        "middle": {
            "code": "c.middle_code",
            "name": "c.middle_name",
            "select": [
                "c.major_code AS major_code",
                "c.middle_code AS middle_code",
                "'' AS small_code",
                "'' AS sub_code",
            ],
            "filters": ["major_code"],
            "order": "c.middle_code",
            "group": "c.major_code, c.middle_code, c.middle_name",
        },
        "small": {
            "code": "c.small_code",
            "name": "c.small_name",
            "select": [
                "c.major_code AS major_code",
                "c.middle_code AS middle_code",
                "c.small_code AS small_code",
                "'' AS sub_code",
            ],
            "filters": ["major_code", "middle_code"],
            "order": "c.small_code",
            "group": "c.major_code, c.middle_code, c.small_code, c.small_name",
        },
        "sub": {
            "code": "c.sub_code",
            "name": "c.sub_name",
            "select": [
                "c.major_code AS major_code",
                "c.middle_code AS middle_code",
                "c.small_code AS small_code",
                "c.sub_code AS sub_code",
            ],
            "filters": ["major_code", "middle_code", "small_code"],
            "order": "c.sub_code",
            "group": "c.major_code, c.middle_code, c.small_code, c.sub_code, c.sub_name",
        },
    }
    if level not in levels:
        raise ValueError(f"unsupported taxonomy level: {level}")

    spec = levels[level]
    clauses: list[str] = []
    values: list[str] = []
    for field in spec["filters"]:
        value = first(params, field)
        if value:
            clauses.append(f"c.{field} = ?")
            values.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = safe_limit(params, default=500, maximum=1200)
    select_codes = ", ".join(spec["select"])
    conn = connect_db_for_read(db_path, required_tables=DASHBOARD_LOOKUP_TABLES)
    rows = conn.execute(
        f"""
        SELECT
            {select_codes},
            {spec["code"]} AS code,
            {spec["name"]} AS name,
            COUNT(DISTINCT c.classification_id) AS classification_count,
            COUNT(DISTINCT cu.unit_code) AS unit_count,
            COUNT(DISTINCT ce.element_id) AS element_count,
            COUNT(DISTINCT CASE WHEN ce.api_match_status = 'matched' THEN ce.element_id END) AS element_matched,
            COUNT(DISTINCT CASE WHEN ce.api_match_status = 'not_collected' THEN ce.element_id END) AS element_not_collected,
            COUNT(DISTINCT CASE WHEN ce.api_match_status IN ('api_failed', 'no_data') THEN ce.element_id END) AS element_problem
        FROM classifications c
        LEFT JOIN competency_units cu ON cu.classification_id = c.classification_id
        LEFT JOIN competency_elements ce ON ce.unit_code = cu.unit_code
        {where}
        GROUP BY {spec["group"]}
        ORDER BY {spec["order"]}
        LIMIT ?
        """,
        [*values, limit],
    ).fetchall()
    nodes = []
    for row in rows:
        node = dict(row)
        total = int(node["element_count"] or 0)
        matched = int(node["element_matched"] or 0)
        node["element_percent"] = percent(matched, total) if total else 0
        nodes.append(node)
    conn.close()
    return {"level": level, "nodes": nodes}


def get_ontology(db_path: Path, params: dict[str, list[str]]) -> dict:
    clauses, values = classification_filters(params, "c")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    unit_limit = safe_limit(params, default=20, maximum=50)
    conn = connect_db_for_read(db_path, required_tables=DASHBOARD_ONTOLOGY_LOOKUP_TABLES)
    units = conn.execute(
        f"""
        SELECT
            cu.unit_code,
            COALESCE(cu.unit_name_refined, cu.unit_name_raw) AS unit_name,
            cu.unit_level_raw,
            cu.review_status,
            cu.api_match_status,
            c.major_code, c.middle_code, c.small_code, c.sub_code,
            c.major_name, c.middle_name, c.small_name, c.sub_name
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where}
        ORDER BY cu.unit_code
        LIMIT ?
        """,
        [*values, unit_limit],
    ).fetchall()
    result_units = []
    for unit in units:
        elements = conn.execute(
            """
            SELECT
                ce.element_id,
                ce.element_no,
                COALESCE(ce.element_name_refined, ce.element_name_raw) AS element_name,
                ce.review_status,
                ce.api_match_status
            FROM competency_elements ce
            WHERE ce.unit_code = ?
            ORDER BY CAST(ce.element_no AS INTEGER), ce.element_id
            """,
            (unit["unit_code"],),
        ).fetchall()
        result_elements = []
        for element in elements:
            criteria = conn.execute(
                """
                SELECT
                    pc.criteria_id,
                    pc.criteria_no,
                    pc.criteria_text_raw,
                    pc.criteria_text_refined,
                    pc.review_status
                FROM performance_criteria pc
                WHERE pc.element_id = ?
                ORDER BY CAST(pc.criteria_no AS INTEGER), pc.criteria_id
                """,
                (element["element_id"],),
            ).fetchall()
            ksa_rows = conn.execute(
                """
                SELECT
                    ki.ksa_id,
                    ki.ksa_type_code,
                    ki.ksa_type_name,
                    ki.ksa_no,
                    ki.ksa_text_raw,
                    ki.ksa_text_refined,
                    ki.review_status,
                    oc.concept_id,
                    oc.concept_name,
                    oc.definition,
                    oc.definition_status,
                    oc.relation_status,
                    oc.review_status AS concept_review_status
                FROM ksa_items ki
                LEFT JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
                LEFT JOIN ontology_concepts oc ON oc.concept_id = kcl.concept_id
                WHERE ki.element_id = ?
                ORDER BY ki.ksa_type_code, CAST(ki.ksa_no AS INTEGER), ki.ksa_id
                """,
                (element["element_id"],),
            ).fetchall()
            ksa_groups: dict[str, list[dict]] = {}
            for row in ksa_rows:
                ksa_groups.setdefault(row["ksa_type_name"], []).append(dict(row))
            result_elements.append(
                {
                    **dict(element),
                    "criteria": [dict(row) for row in criteria],
                    "ksa_groups": ksa_groups,
                }
            )
        result_units.append({**dict(unit), "elements": result_elements})
    total_units = count_query(
        conn,
        f"""
        SELECT COUNT(*)
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where}
        """,
        values,
    )
    conn.close()
    return {
        "unit_limit": unit_limit,
        "total_units": total_units,
        "units": result_units,
    }


def concept_scope_filter(params: dict[str, list[str]]) -> tuple[str, list[str]]:
    scope_clauses, scope_values = classification_filters(params, "c")
    if not scope_clauses:
        return "", []
    scope_sql = " AND ".join(scope_clauses)
    clause = f"""
    EXISTS (
      SELECT 1
      FROM ksa_concept_links kcl
      JOIN ksa_items ki ON ki.ksa_id = kcl.ksa_id
      JOIN competency_elements ce ON ce.element_id = ki.element_id
      JOIN competency_units cu ON cu.unit_code = ce.unit_code
      JOIN classifications c ON c.classification_id = cu.classification_id
      WHERE kcl.concept_id = oc.concept_id
        AND {scope_sql}
    )
    """
    return clause, scope_values


def get_ontology_status(db_path: Path, params: dict[str, list[str]]) -> dict:
    conn = connect_db_for_read(db_path, required_tables=DASHBOARD_ONTOLOGY_LOOKUP_TABLES)
    scope_clause, scope_values = concept_scope_filter(params)
    where_scope = f" AND {scope_clause}" if scope_clause else ""
    types = [
        ("knowledge", "지식"),
        ("skill", "기술"),
        ("attitude", "태도"),
    ]
    statuses = []
    for concept_type, label in types:
        base_values = [concept_type, *scope_values]
        total = count_query(
            conn,
            f"SELECT COUNT(*) FROM ontology_concepts oc WHERE oc.concept_type = ? {where_scope}",
            base_values,
        )
        definition_done = count_query(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concepts oc
            WHERE oc.concept_type = ?
              AND oc.definition IS NOT NULL
              AND TRIM(oc.definition) <> ''
              {where_scope}
            """,
            base_values,
        )
        relation_done = count_query(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concepts oc
            WHERE oc.concept_type = ?
              AND EXISTS (
                SELECT 1
                FROM ontology_concept_relations rel
                WHERE rel.source_concept_id = oc.concept_id
              )
              {where_scope}
            """,
            base_values,
        )
        reviewed = count_query(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concepts oc
            WHERE oc.concept_type = ?
              AND oc.review_status = 'human_reviewed'
              {where_scope}
            """,
            base_values,
        )
        duplicate_like = count_query(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concepts oc
            WHERE oc.concept_type = ?
              AND (
                SELECT COUNT(*)
                FROM ontology_concept_aliases alias
                WHERE alias.concept_id = oc.concept_id
              ) > 1
              {where_scope}
            """,
            base_values,
        )
        statuses.append(
            {
                "concept_type": concept_type,
                "label": label,
                "total": total,
                "definition_done": definition_done,
                "definition_missing": max(total - definition_done, 0),
                "relation_done": relation_done,
                "relation_missing": max(total - relation_done, 0),
                "reviewed": reviewed,
                "duplicate_like": duplicate_like,
            }
        )
    conn.close()
    return {"statuses": statuses}


def get_concepts(db_path: Path, params: dict[str, list[str]]) -> dict:
    concept_type = first(params, "concept_type", "knowledge")
    state = first(params, "state", "definition_missing")
    limit = safe_limit(params, default=100, maximum=300)
    clauses = ["oc.concept_type = ?"]
    values: list[str | int] = [concept_type]
    scope_clause, scope_values = concept_scope_filter(params)
    if scope_clause:
        clauses.append(scope_clause)
        values.extend(scope_values)
    if state == "definition_missing":
        clauses.append("(oc.definition IS NULL OR TRIM(oc.definition) = '')")
    elif state == "relation_missing":
        clauses.append(
            """
            NOT EXISTS (
              SELECT 1
              FROM ontology_concept_relations rel
              WHERE rel.source_concept_id = oc.concept_id
            )
            """
        )
    elif state == "reviewed":
        clauses.append("oc.review_status = 'human_reviewed'")
    elif state == "duplicates":
        clauses.append(
            """
            (
              SELECT COUNT(*)
              FROM ontology_concept_aliases alias
              WHERE alias.concept_id = oc.concept_id
            ) > 1
            """
        )
    where = "WHERE " + " AND ".join(clauses)
    conn = connect_db_for_read(db_path, required_tables=DASHBOARD_ONTOLOGY_LOOKUP_TABLES)
    rows = conn.execute(
        f"""
        SELECT
            oc.concept_id,
            oc.concept_name,
            oc.concept_type,
            oc.definition,
            oc.definition_status,
            oc.relation_status,
            oc.review_status,
            COUNT(DISTINCT alias.alias_id) AS alias_count,
            COUNT(DISTINCT rel.relation_id) AS relation_count,
            MIN(kcl.ksa_id) AS sample_ksa_id
        FROM ontology_concepts oc
        LEFT JOIN ontology_concept_aliases alias ON alias.concept_id = oc.concept_id
        LEFT JOIN ontology_concept_relations rel ON rel.source_concept_id = oc.concept_id
        LEFT JOIN ksa_concept_links kcl ON kcl.concept_id = oc.concept_id
        {where}
        GROUP BY oc.concept_id
        ORDER BY oc.review_status, oc.concept_name
        LIMIT ?
        """,
        [*values, limit],
    ).fetchall()
    total = count_query(
        conn,
        f"SELECT COUNT(*) FROM ontology_concepts oc {where}",
        values,
    )
    conn.close()
    return {
        "concept_type": concept_type,
        "state": state,
        "total": total,
        "concepts": [dict(row) for row in rows],
    }


def json_object(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"_raw": value}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def get_recommendation_runs(db_path: Path, params: dict[str, list[str]]) -> dict:
    limit = safe_limit(params, default=25, maximum=100)
    clauses: list[str] = []
    values: list[str] = []
    query = first(params, "query")
    target_source_key = first(params, "target_source_key")
    if query:
        clauses.append("query LIKE ?")
        values.append(f"%{query}%")
    if target_source_key:
        clauses.append("target_source_key = ?")
        values.append(target_source_key)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = connect_db_for_read(db_path, required_tables=DASHBOARD_LOOKUP_TABLES)
    rows = conn.execute(
        f"""
        SELECT *
        FROM education_recommendation_runs
        {where}
        ORDER BY run_id DESC
        LIMIT ?
        """,
        [*values, limit],
    ).fetchall()
    total = count_query(conn, f"SELECT COUNT(*) FROM education_recommendation_runs {where}", values)
    conn.close()
    runs = []
    for row in rows:
        runs.append(
            {
                "run_id": row["run_id"],
                "query": row["query"],
                "target_source_key": row["target_source_key"],
                "created_at": row["created_at"],
                "request": json_object(row["request_payload"]),
                "target": json_object(row["target_payload"]),
                "summary": json_object(row["summary_payload"]),
                "audit": json_object(row["audit_payload"]),
            }
        )
    return {"total": total, "runs": runs}


def get_recommendation_detail(db_path: Path, params: dict[str, list[str]]) -> dict:
    run_id = first(params, "run_id")
    if not run_id:
        raise ValueError("run_id is required")
    conn = connect_db_for_read(db_path, required_tables=DASHBOARD_LOOKUP_TABLES)
    run = conn.execute(
        "SELECT * FROM education_recommendation_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if run is None:
        conn.close()
        return {"error": "not_found", "run_id": run_id}
    item_rows = conn.execute(
        """
        SELECT *
        FROM education_recommendation_items
        WHERE run_id = ?
        ORDER BY rank
        """,
        (run_id,),
    ).fetchall()
    evidence_rows = conn.execute(
        """
        SELECT *
        FROM education_recommendation_evidence
        WHERE run_id = ?
        ORDER BY item_id, evidence_id
        """,
        (run_id,),
    ).fetchall()
    conn.close()
    return {
        "run": {
            "run_id": run["run_id"],
            "query": run["query"],
            "target_source_key": run["target_source_key"],
            "created_at": run["created_at"],
            "request": json_object(run["request_payload"]),
            "target": json_object(run["target_payload"]),
            "summary": json_object(run["summary_payload"]),
            "audit": json_object(run["audit_payload"]),
        },
        "items": [
            {
                **dict(row),
                "payload": json_object(row["recommendation_payload"]),
            }
            for row in item_rows
        ],
        "evidence": [dict(row) for row in evidence_rows],
    }


def keyword_clause(params: dict[str, list[str]], fields: list[str]) -> tuple[str, list[str]]:
    keyword = first(params, "keyword")
    if not keyword:
        return "", []
    clause = "(" + " OR ".join(f"{field} LIKE ?" for field in fields) + ")"
    return clause, [f"%{keyword}%" for _ in fields]


def state_clause(kind: str, state: str, alias: str) -> str:
    if state == "api_matched":
        return f"{alias}.api_match_status = 'matched'"
    if state == "api_not_collected":
        return f"{alias}.api_match_status = 'not_collected'"
    if state == "api_problem":
        return f"{alias}.api_match_status IN ('api_failed', 'no_data')"
    if state == "raw" and kind == "criteria":
        return "(pc.criteria_text_refined IS NULL OR TRIM(pc.criteria_text_refined) = '')"
    if state == "raw" and kind == "ksa":
        return "(ki.ksa_text_refined IS NULL OR TRIM(ki.ksa_text_refined) = '')"
    if state == "refined" and kind == "criteria":
        return "(pc.criteria_text_refined IS NOT NULL AND TRIM(pc.criteria_text_refined) <> '')"
    if state == "refined" and kind == "ksa":
        return "(ki.ksa_text_refined IS NOT NULL AND TRIM(ki.ksa_text_refined) <> '')"
    return ""


def get_items(db_path: Path, params: dict[str, list[str]]) -> dict:
    kind = first(params, "kind", "classification")
    state = first(params, "state", "processed")
    limit = safe_limit(params)
    conn = connect_db_for_read(db_path, required_tables=DASHBOARD_LOOKUP_TABLES)
    if kind == "classification":
        where, values = scoped_where(params, "c")
        rows = conn.execute(
            f"""
            SELECT 'classification' AS kind, c.classification_id AS id,
                   c.major_code || '-' || c.middle_code || '-' || c.small_code || '-' || c.sub_code AS code,
                   c.major_name || ' > ' || c.middle_name || ' > ' || c.small_name AS context,
                   c.sub_name AS title, COALESCE(c.duty_def_refined, c.duty_def_api, '') AS body,
                   c.review_status AS status, c.api_usg_yn AS api_status
            FROM classifications c
            {where}
            ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()
        total = count_query(conn, f"SELECT COUNT(*) FROM classifications c {where}", values)
    elif kind == "unit":
        extra = []
        state_sql = state_clause(kind, state, "cu")
        if state_sql:
            extra.append(state_sql)
        kw, kw_vals = keyword_clause(params, ["cu.unit_code", "cu.unit_name_raw", "cu.api_definition"])
        if kw:
            extra.append(kw)
        where, values = scoped_where(params, "c", extra)
        values.extend(kw_vals)
        rows = conn.execute(
            f"""
            SELECT 'unit' AS kind, cu.unit_code AS id, cu.unit_code AS code,
                   c.major_name || ' > ' || c.middle_name || ' > ' || c.small_name || ' > ' || c.sub_name AS context,
                   COALESCE(cu.unit_name_refined, cu.unit_name_raw) AS title,
                   COALESCE(cu.api_definition_refined, cu.api_definition, '') AS body,
                   cu.review_status AS status, cu.api_match_status AS api_status
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            {where}
            ORDER BY cu.unit_code
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()
        total = count_query(
            conn,
            f"""
            SELECT COUNT(*)
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            {where}
            """,
            values,
        )
    elif kind == "element":
        extra = []
        state_sql = state_clause(kind, state, "ce")
        if state_sql:
            extra.append(state_sql)
        kw, kw_vals = keyword_clause(params, ["ce.element_name_raw", "ce.api_element_name", "ce.unit_code"])
        if kw:
            extra.append(kw)
        where, values = scoped_where(params, "c", extra)
        values.extend(kw_vals)
        rows = conn.execute(
            f"""
            SELECT 'element' AS kind, ce.element_id AS id, ce.unit_code || ' #' || ce.element_no AS code,
                   cu.unit_code || ' ' || cu.unit_name_raw AS context,
                   COALESCE(ce.element_name_refined, ce.element_name_raw) AS title,
                   COALESCE(ce.api_element_name, '') AS body,
                   ce.review_status AS status, ce.api_match_status AS api_status
            FROM competency_elements ce
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            {where}
            ORDER BY ce.unit_code, CAST(ce.element_no AS INTEGER), ce.element_id
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()
        total = count_query(
            conn,
            f"""
            SELECT COUNT(*)
            FROM competency_elements ce
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            {where}
            """,
            values,
        )
    elif kind == "criteria":
        extra = []
        state_sql = state_clause(kind, state, "pc")
        if state_sql:
            extra.append(state_sql)
        kw, kw_vals = keyword_clause(params, ["pc.criteria_text_raw", "ce.element_name_raw", "cu.unit_name_raw"])
        if kw:
            extra.append(kw)
        where, values = scoped_where(params, "c", extra)
        values.extend(kw_vals)
        rows = conn.execute(
            f"""
            SELECT 'criteria' AS kind, pc.criteria_id AS id, '수행준거 ' || pc.criteria_no AS code,
                   cu.unit_code || ' ' || cu.unit_name_raw || ' > ' || ce.element_name_raw AS context,
                   ce.element_name_raw AS title,
                   COALESCE(pc.criteria_text_refined, pc.criteria_text_raw) AS body,
                   pc.review_status AS status, ce.api_match_status AS api_status
            FROM performance_criteria pc
            JOIN competency_elements ce ON ce.element_id = pc.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            {where}
            ORDER BY cu.unit_code, ce.element_no, CAST(pc.criteria_no AS INTEGER), pc.criteria_id
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()
        total = count_query(
            conn,
            f"""
            SELECT COUNT(*)
            FROM performance_criteria pc
            JOIN competency_elements ce ON ce.element_id = pc.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            {where}
            """,
            values,
        )
    elif kind == "ksa":
        extra = []
        state_sql = state_clause(kind, state, "ki")
        if state_sql:
            extra.append(state_sql)
        kw, kw_vals = keyword_clause(params, ["ki.ksa_text_raw", "ki.ksa_type_name", "ce.element_name_raw", "cu.unit_name_raw"])
        if kw:
            extra.append(kw)
        where, values = scoped_where(params, "c", extra)
        values.extend(kw_vals)
        rows = conn.execute(
            f"""
            SELECT 'ksa' AS kind, ki.ksa_id AS id, ki.ksa_type_name || ' ' || ki.ksa_no AS code,
                   cu.unit_code || ' ' || cu.unit_name_raw || ' > ' || ce.element_name_raw AS context,
                   ce.element_name_raw AS title,
                   COALESCE(ki.ksa_text_refined, ki.ksa_text_raw) AS body,
                   ki.review_status AS status, ce.api_match_status AS api_status
            FROM ksa_items ki
            JOIN competency_elements ce ON ce.element_id = ki.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            {where}
            ORDER BY cu.unit_code, ce.element_no, ki.ksa_type_code, CAST(ki.ksa_no AS INTEGER), ki.ksa_id
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()
        total = count_query(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ksa_items ki
            JOIN competency_elements ce ON ce.element_id = ki.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            {where}
            """,
            values,
        )
    elif kind == "sqf":
        clauses: list[str] = []
        values: list[str] = []
        major_code = first(params, "major_code")
        if major_code:
            clauses.append("sd.ncs_lclas_cd = ?")
            values.append(major_code)
        if state == "mvp":
            clauses.extend(
                [
                    "sd.ncs_lclas_cd = '02'",
                    "sd.sqf_field_name = '경영관리'",
                    "sd.job_name = '경영지원'",
                ]
            )
        elif state == "training":
            clauses.append(
                """
                sd.duty_education_training IS NOT NULL
                AND TRIM(sd.duty_education_training) <> ''
                AND TRIM(sd.duty_education_training) <> '-'
                """
            )
        kw, kw_vals = keyword_clause(
            params,
            [
                "sd.ncs_lclas_name",
                "sd.sqf_field_name",
                "sd.job_name",
                "sd.duty_name",
                "sd.duty_definition",
                "sd.duty_qualification",
                "sd.duty_career",
            ],
        )
        if kw:
            clauses.append(kw)
            values.extend(kw_vals)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"""
            SELECT 'sqf' AS kind, sd.source_key AS id,
                   sd.ncs_lclas_cd || ' ' || sd.ncs_lclas_name AS code,
                   sd.sqf_field_name || ' > ' || sd.job_name AS context,
                   sd.duty_name || CASE WHEN sd.duty_level <> '' THEN ' / Level ' || sd.duty_level ELSE '' END AS title,
                   COALESCE(sd.duty_definition, sd.duty_level_name, '') AS body,
                   CASE
                     WHEN sd.duty_definition IS NOT NULL AND TRIM(sd.duty_definition) <> '' THEN 'mapped_source'
                     ELSE 'needs_review'
                   END AS status,
                   CASE
                     WHEN sd.duty_education_training IS NOT NULL
                       AND TRIM(sd.duty_education_training) <> ''
                       AND TRIM(sd.duty_education_training) <> '-'
                     THEN 'training'
                     ELSE 'no_training'
                   END AS api_status
            FROM sqf_duties sd
            {where}
            ORDER BY sd.ncs_lclas_cd, sd.sqf_field_name, sd.job_name, CAST(sd.duty_level AS INTEGER), sd.duty_name
            LIMIT ?
            """,
            [*values, limit],
        ).fetchall()
        total = count_query(conn, f"SELECT COUNT(*) FROM sqf_duties sd {where}", values)
    elif kind == "quality":
        issue_scope, issue_scope_values = quality_issue_scope_filter(params)
        issue_where = "WHERE qi.resolved_at IS NULL"
        if issue_scope:
            issue_where += f" AND {issue_scope}"
        rows = conn.execute(
            f"""
            SELECT 'quality' AS kind, qi.issue_id AS id, qi.target_type || ':' || qi.target_id AS code,
                   qi.issue_type AS context, qi.severity AS title, qi.issue_detail AS body,
                   qi.severity AS status, '' AS api_status
            FROM quality_issues qi
            {issue_where}
            ORDER BY CASE qi.severity WHEN 'error' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END, qi.issue_id
            LIMIT ?
            """,
            [*issue_scope_values, limit],
        ).fetchall()
        total = count_query(
            conn,
            f"SELECT COUNT(*) FROM quality_issues qi {issue_where}",
            issue_scope_values,
        )
    else:
        conn.close()
        raise ValueError(f"unsupported kind: {kind}")
    conn.close()
    return {"kind": kind, "state": state, "total": total, "items": [dict(row) for row in rows]}


def get_item_detail(db_path: Path, params: dict[str, list[str]]) -> dict:
    kind = first(params, "kind")
    item_id = first(params, "id")
    if not kind or not item_id:
        raise ValueError("kind and id are required")
    if kind == "ksa":
        required_tables = DASHBOARD_ONTOLOGY_LOOKUP_TABLES
    elif kind == "sqf":
        required_tables = ("sqf_duties",)
    elif kind == "quality":
        required_tables = ("quality_issues",)
    else:
        required_tables = DASHBOARD_LOOKUP_TABLES
    conn = connect_db_for_read(db_path, required_tables=required_tables)
    row = None
    item: dict
    if kind == "classification":
        row = conn.execute(
            """
            SELECT c.*, c.major_name || ' > ' || c.middle_name || ' > ' || c.small_name AS context
            FROM classifications c
            WHERE c.classification_id = ?
            """,
            (item_id,),
        ).fetchone()
        if row:
            item = {
                "kind": kind,
                "id": item_id,
                "context": row["context"],
                "title_raw": row["sub_name"],
                "title_refined": row["sub_name"],
                "body_raw": row["duty_def_api"] or "",
                "body_refined": row["duty_def_refined"] or "",
                "can_refine_title": False,
                "can_refine_body": True,
                "status": row["review_status"],
            }
        else:
            item = {}
    elif kind == "unit":
        row = conn.execute(
            """
            SELECT cu.*, c.major_name || ' > ' || c.middle_name || ' > ' || c.small_name || ' > ' || c.sub_name AS context
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE cu.unit_code = ?
            """,
            (item_id,),
        ).fetchone()
        if row:
            item = {
                "kind": kind,
                "id": item_id,
                "context": row["context"],
                "title_raw": row["unit_name_raw"],
                "title_refined": row["unit_name_refined"] or "",
                "body_raw": row["api_definition"] or "",
                "body_refined": row["api_definition_refined"] or "",
                "can_refine_title": True,
                "can_refine_body": True,
                "status": row["review_status"],
                "api_status": row["api_match_status"],
            }
        else:
            item = {}
    elif kind == "element":
        row = conn.execute(
            """
            SELECT ce.*, cu.unit_name_raw, c.major_name, c.middle_name, c.small_name, c.sub_name
            FROM competency_elements ce
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE ce.element_id = ?
            """,
            (item_id,),
        ).fetchone()
        if row:
            item = {
                "kind": kind,
                "id": item_id,
                "context": f"{row['major_name']} > {row['middle_name']} > {row['small_name']} > {row['sub_name']}\n{row['unit_code']} {row['unit_name_raw']}",
                "title_raw": row["element_name_raw"],
                "title_refined": row["element_name_refined"] or "",
                "body_raw": row["api_element_name"] or "",
                "body_refined": row["api_element_name"] or "",
                "can_refine_title": True,
                "can_refine_body": False,
                "status": row["review_status"],
                "api_status": row["api_match_status"],
            }
        else:
            item = {}
    elif kind == "criteria":
        row = conn.execute(
            """
            SELECT pc.*, ce.element_name_raw, cu.unit_code, cu.unit_name_raw, c.major_name, c.middle_name, c.small_name, c.sub_name
            FROM performance_criteria pc
            JOIN competency_elements ce ON ce.element_id = pc.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE pc.criteria_id = ?
            """,
            (item_id,),
        ).fetchone()
        if row:
            item = {
                "kind": kind,
                "id": item_id,
                "context": f"{row['major_name']} > {row['middle_name']} > {row['small_name']} > {row['sub_name']}\n{row['unit_code']} {row['unit_name_raw']}\n{row['element_name_raw']}",
                "title_raw": f"수행준거 {row['criteria_no']}",
                "title_refined": f"수행준거 {row['criteria_no']}",
                "body_raw": row["criteria_text_raw"],
                "body_refined": row["criteria_text_refined"] or "",
                "can_refine_title": False,
                "can_refine_body": True,
                "status": row["review_status"],
            }
        else:
            item = {}
    elif kind == "ksa":
        row = conn.execute(
            """
            SELECT
                ki.*,
                ce.element_name_raw,
                cu.unit_code,
                cu.unit_name_raw,
                c.major_name,
                c.middle_name,
                c.small_name,
                c.sub_name,
                oc.concept_id,
                oc.concept_name,
                oc.definition,
                oc.definition_status,
                oc.relation_status,
                oc.review_status AS concept_review_status
            FROM ksa_items ki
            JOIN competency_elements ce ON ce.element_id = ki.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            LEFT JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
            LEFT JOIN ontology_concepts oc ON oc.concept_id = kcl.concept_id
            WHERE ki.ksa_id = ?
            """,
            (item_id,),
        ).fetchone()
        if row:
            aliases = []
            relations = {"parent": [], "child": [], "related": []}
            related_criteria = []
            if row["concept_id"]:
                aliases = [
                    item["alias_text"]
                    for item in conn.execute(
                        """
                        SELECT alias_text
                        FROM ontology_concept_aliases
                        WHERE concept_id = ?
                        ORDER BY alias_text
                        """,
                        (row["concept_id"],),
                    ).fetchall()
                ]
                for rel_type in relations:
                    relations[rel_type] = [
                        item["concept_name"]
                        for item in conn.execute(
                            """
                            SELECT target.concept_name
                            FROM ontology_concept_relations rel
                            JOIN ontology_concepts target ON target.concept_id = rel.target_concept_id
                            WHERE rel.source_concept_id = ? AND rel.relation_type = ?
                            ORDER BY target.concept_name
                            """,
                            (row["concept_id"], rel_type),
                        ).fetchall()
                    ]
                related_criteria = [
                    dict(item)
                    for item in conn.execute(
                        """
                        SELECT DISTINCT pc.criteria_id, pc.criteria_no, pc.criteria_text_raw
                        FROM criteria_concept_links ccl
                        JOIN performance_criteria pc ON pc.criteria_id = ccl.criteria_id
                        WHERE ccl.concept_id = ?
                        ORDER BY CAST(pc.criteria_no AS INTEGER), pc.criteria_id
                        LIMIT 100
                        """,
                        (row["concept_id"],),
                    ).fetchall()
                ]
            item = {
                "kind": kind,
                "id": item_id,
                "context": f"{row['major_name']} > {row['middle_name']} > {row['small_name']} > {row['sub_name']}\n{row['unit_code']} {row['unit_name_raw']}\n{row['element_name_raw']}",
                "title_raw": f"{row['ksa_type_name']} {row['ksa_no']}",
                "title_refined": row["concept_name"] or row["ksa_text_refined"] or row["ksa_text_raw"],
                "body_raw": row["ksa_text_raw"],
                "body_refined": row["definition"] or "",
                "can_refine_title": True,
                "can_refine_body": True,
                "status": row["review_status"],
                "concept_id": row["concept_id"],
                "concept_name": row["concept_name"],
                "concept_type": {"지식": "knowledge", "기술": "skill", "태도": "attitude"}.get(row["ksa_type_name"], "knowledge"),
                "definition_status": row["definition_status"] or "missing",
                "relation_status": row["relation_status"] or "unlinked",
                "concept_review_status": row["concept_review_status"] or "raw",
                "aliases": aliases,
                "relations": relations,
                "related_criteria": related_criteria,
            }
        else:
            item = {}
    elif kind == "sqf":
        row = conn.execute(
            """
            SELECT *
            FROM sqf_duties
            WHERE source_key = ?
            """,
            (item_id,),
        ).fetchone()
        if row:
            evidence = [
                f"직무정의: {row['duty_definition'] or ''}",
                f"직무수준 정의: {row['duty_level_name'] or ''}",
                f"자율성과 책임성: {row['autonomy_responsibility'] or ''}",
                f"교육훈련: {row['duty_education_training'] or ''}",
                f"자격: {row['duty_qualification'] or ''}",
                f"경력: {row['duty_career'] or ''}",
                f"면허: {row['duty_license'] or ''}",
                f"비고: {row['duty_remark'] or ''}",
            ]
            item = {
                "kind": kind,
                "id": item_id,
                "context": (
                    f"{row['ncs_lclas_cd']} {row['ncs_lclas_name']}\n"
                    f"{row['sqf_field_name']} > {row['job_name']}"
                ),
                "title_raw": f"{row['duty_name']} / Level {row['duty_level']}",
                "title_refined": f"{row['duty_name']} / Level {row['duty_level']}",
                "body_raw": "\n".join(evidence),
                "body_refined": "",
                "can_refine_title": False,
                "can_refine_body": False,
                "status": "mapped_source" if row["duty_definition"] else "needs_review",
                "api_status": "training" if row["duty_education_training"] else "no_training",
            }
        else:
            item = {}
    elif kind == "quality":
        row = conn.execute("SELECT * FROM quality_issues WHERE issue_id = ?", (item_id,)).fetchone()
        if row:
            item = {
                "kind": kind,
                "id": item_id,
                "context": f"{row['target_type']}:{row['target_id']}",
                "title_raw": row["issue_type"],
                "title_refined": row["issue_type"],
                "body_raw": row["issue_detail"],
                "body_refined": row["suggested_action"] or "",
                "can_refine_title": False,
                "can_refine_body": False,
                "status": row["severity"],
            }
        else:
            item = {}
    else:
        conn.close()
        raise ValueError(f"unsupported kind: {kind}")
    conn.close()
    if not item:
        return {"error": "not_found", "kind": kind, "id": item_id}
    return {"item": item}


def split_lines(value: object) -> list[str]:
    if value is None:
        return []
    raw = str(value).replace(",", "\n").splitlines()
    items: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = " ".join(item.strip().split())
        key = normalize_concept_key(text)
        if text and key not in seen:
            items.append(text)
            seen.add(key)
    return items


def get_or_create_concept(conn, concept_type: str, concept_name: str) -> int:
    name = " ".join(concept_name.strip().split())
    if not name:
        raise ValueError("concept name is required")
    key = normalize_concept_key(name)
    row = conn.execute(
        """
        SELECT concept_id
        FROM ontology_concepts
        WHERE concept_type = ? AND normalized_key = ?
        """,
        (concept_type, key),
    ).fetchone()
    if row:
        return int(row["concept_id"])
    timestamp = now_utc()
    cur = conn.execute(
        """
        INSERT INTO ontology_concepts(
            concept_name, normalized_key, concept_type,
            definition_status, relation_status, review_status,
            created_at, updated_at
        ) VALUES (?, ?, ?, 'missing', 'unlinked', 'raw', ?, ?)
        """,
        (name, key, concept_type, timestamp, timestamp),
    )
    return int(cur.lastrowid)


def save_concept_aliases(conn, concept_id: int, aliases: list[str]) -> None:
    timestamp = now_utc()
    for alias in aliases:
        conn.execute(
            """
            INSERT OR IGNORE INTO ontology_concept_aliases(
                concept_id, alias_text, normalized_alias_key, alias_source, created_at
            ) VALUES (?, ?, ?, 'manual', ?)
            """,
            (concept_id, alias, normalize_concept_key(alias), timestamp),
        )


def replace_concept_relations(
    conn,
    *,
    source_concept_id: int,
    concept_type: str,
    relation_type: str,
    target_names: list[str],
) -> None:
    conn.execute(
        """
        DELETE FROM ontology_concept_relations
        WHERE source_concept_id = ? AND relation_type = ?
        """,
        (source_concept_id, relation_type),
    )
    timestamp = now_utc()
    for target_name in target_names:
        target_id = get_or_create_concept(conn, concept_type, target_name)
        if target_id == source_concept_id:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO ontology_concept_relations(
                source_concept_id, relation_type, target_concept_id,
                relation_label, review_status, created_at
            ) VALUES (?, ?, ?, ?, 'human_reviewed', ?)
            """,
            (source_concept_id, relation_type, target_id, relation_type, timestamp),
        )


DASHBOARD_AUTOMATED_REVIEWER_IDS = {
    "",
    "dashboard",
    "dashboard_click",
    "local_operator",
    "mcp",
    "automation",
    "automated_eval_gate",
    "system",
}


SYNTHETIC_REVIEW_PACKET_PREFIXES = (
    "ksa_review_dashboard_one_click_v1:",
    "ksa_definition_dashboard_review_v1:",
)


TRUSTED_REVIEW_PACKET_EXTENSIONS = (".json",)
TRUSTED_REVIEW_PACKET_FILENAME_MARKERS = (
    "decision_audit",
    "review_decision_audit",
    "claim_decision_audit",
)
TRUSTED_REVIEW_DECISION_AUDIT_SCHEMAS = {
    "aihr_provenance_reconfirmation_decision_audit_v1",
    "ncs_dashboard_review_decision_audit_v1",
    "ncs_ksa_definition_review_decision_audit_v1",
}
TRUSTED_REVIEW_DECISION_AUDIT_FORMATS = {
    "ncs-sqf-report-claim-decision-audit-v1",
}
TRUSTED_REVIEW_BLANK_DECISIONS = {"", "blank", "pending", "defer", "deferred"}


def manual_preprocess_reference_tokens(kind: str, item_id: str) -> tuple[str, ...]:
    normalized_kind = str(kind or "").strip().lower()
    normalized_id = str(item_id or "").strip()
    if not normalized_id:
        return ()
    if normalized_kind == "classification":
        return (f"classification:{normalized_id}",)
    if normalized_kind == "unit":
        return (f"unit:{normalized_id}",)
    if normalized_kind == "element":
        return (f"element:{normalized_id}",)
    if normalized_kind == "criteria":
        return (f"criteria:{normalized_id}",)
    if normalized_kind == "ksa":
        return (f"ksa_id:{normalized_id}",)
    if normalized_kind == "refinement":
        return (f"refinement_job:{normalized_id}",)
    return ()


def refinement_target_packet_content_blocker(target_type: str) -> str:
    normalized_target_type = str(target_type or "").strip().lower()
    if normalized_target_type in {"classification", "unit", "element", "criteria", "ksa"}:
        return f"refinement_review_requires_packet_row_for_{normalized_target_type}_decision"
    return "refinement_review_requires_packet_row_for_target_decision"


def _split_review_packet_reference(source_decision_packet: str | None) -> tuple[str, str]:
    value = (source_decision_packet or "").strip()
    artifact_ref, _, fragment = value.partition("#")
    return artifact_ref.strip(), fragment.strip()


def resolve_review_packet_artifact_path(source_decision_packet: str | None) -> Path | None:
    artifact_ref, _fragment = _split_review_packet_reference(source_decision_packet)
    if not artifact_ref:
        return None
    lowered = artifact_ref.lower()
    if lowered.startswith(SYNTHETIC_REVIEW_PACKET_PREFIXES):
        return None
    return resolve_repo_reports_artifact(
        source_decision_packet,
        root=ROOT,
        extensions=TRUSTED_REVIEW_PACKET_EXTENSIONS,
    )


def _file_sha256(path: Path) -> str:
    return shared_review_packet_sha256(path)


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _trusted_review_artifact_has_human_decision(
    payload: dict[str, Any],
    *,
    expected_reference_tokens: tuple[str, ...],
    reviewer_id: str | None,
) -> bool:
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return False
    normalized_reviewer_id = str(reviewer_id or "").strip()
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_text = _json_text(row)
        if expected_reference_tokens and any(token not in row_text for token in expected_reference_tokens):
            continue
        if normalized_reviewer_id and str(row.get("reviewer_id") or "").strip() != normalized_reviewer_id:
            continue
        decision = str(row.get("decision") or row.get("action") or "").strip().lower()
        if decision in TRUSTED_REVIEW_BLANK_DECISIONS:
            continue
        if row.get("valid") is False:
            continue
        if row.get("completed") is False:
            continue
        if row.get("action_eligible") is False:
            continue
        if any(row.get(field) is True for field in ("status_update_allowed", "db_writes", "approval_claim")):
            continue
        return True
    return False


def trusted_review_decision_artifact_ok(
    packet_path: Path,
    *,
    expected_reference_tokens: tuple[str, ...],
    reviewer_id: str | None,
) -> tuple[bool, str]:
    if not any(marker in packet_path.stem.lower() for marker in TRUSTED_REVIEW_PACKET_FILENAME_MARKERS):
        return False, "trusted_review_packet_filename_not_a_decision_audit"
    try:
        payload = json.loads(packet_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False, "trusted_review_packet_not_json_audit"
    if not isinstance(payload, dict):
        return False, "trusted_review_packet_json_root_not_object"
    schema = str(payload.get("schema") or "").strip()
    format_version = str(payload.get("format_version") or "").strip()
    if schema not in TRUSTED_REVIEW_DECISION_AUDIT_SCHEMAS and format_version not in TRUSTED_REVIEW_DECISION_AUDIT_FORMATS:
        return False, "trusted_review_packet_schema_not_allowlisted"
    if payload.get("ok") is not True:
        return False, "trusted_review_packet_audit_not_ok"
    if any(payload.get(field) is True for field in ("status_update_allowed", "db_writes", "approval_claim")):
        return False, "trusted_review_packet_unsafe_top_level_flags"
    policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
    review_policy = payload.get("review_policy") if isinstance(payload.get("review_policy"), dict) else {}
    if policy.get("db_writes") is True or policy.get("status_update_allowed") is True:
        return False, "trusted_review_packet_unsafe_policy_flags"
    if review_policy.get("db_writes") is True or review_policy.get("status_update_allowed") is True:
        return False, "trusted_review_packet_unsafe_review_policy_flags"
    human_decision_required = (
        payload.get("human_decision_required") is True
        or policy.get("human_decision_required") is True
        or policy.get("requires_explicit_human_decision") is True
        or review_policy.get("human_decision_required") is True
        or review_policy.get("requires_explicit_human_decision") is True
    )
    if not human_decision_required:
        return False, "trusted_review_packet_missing_human_decision_required_gate"
    if not _trusted_review_artifact_has_human_decision(
        payload,
        expected_reference_tokens=expected_reference_tokens,
        reviewer_id=reviewer_id,
    ):
        return False, "trusted_review_packet_missing_completed_matching_human_decision_row"
    return True, "ok"


def trusted_review_packet_blockers(
    *,
    source_decision_packet: str | None,
    source_artifact_hash: str | None,
    packet_missing_blocker: str,
    packet_backed_blocker: str,
    hash_missing_blocker: str,
    hash_format_blocker: str,
    hash_mismatch_blocker: str,
    packet_content_blocker: str,
    expected_reference_tokens: tuple[str, ...] = (),
    reviewer_id: str | None = None,
    packet_audit_blocker: str | None = None,
) -> list[str]:
    blockers: list[str] = []
    packet_path: Path | None = None
    if not (source_decision_packet or "").strip():
        blockers.append(packet_missing_blocker)
    else:
        packet_path = resolve_review_packet_artifact_path(source_decision_packet)
    if source_decision_packet and packet_path is None:
        blockers.append(packet_backed_blocker)
    if not (source_artifact_hash or "").strip():
        blockers.append(hash_missing_blocker)
    elif not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", str(source_artifact_hash)):
        blockers.append(hash_format_blocker)
    elif packet_path is not None:
        expected_hash = "sha256:" + _file_sha256(packet_path)
        if str(source_artifact_hash).lower() != expected_hash:
            blockers.append(hash_mismatch_blocker)
    if packet_path is not None:
        audit_ok, _audit_reason = trusted_review_decision_artifact_ok(
            packet_path,
            expected_reference_tokens=expected_reference_tokens,
            reviewer_id=reviewer_id,
        )
        if not audit_ok:
            blockers.append(packet_audit_blocker or packet_backed_blocker)
    if packet_path is not None and expected_reference_tokens:
        try:
            packet_text = packet_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            packet_text = ""
        if any(token not in packet_text for token in expected_reference_tokens):
            blockers.append(packet_content_blocker)
    return blockers


def manual_preprocess_provenance_blockers(
    *,
    kind: str,
    item_id: str,
    reviewer_id: str,
    source_decision_packet: str | None,
    source_artifact_hash: str | None,
    rationale: str | None,
) -> list[str]:
    blockers: list[str] = []
    if (reviewer_id or "").strip().lower() in DASHBOARD_AUTOMATED_REVIEWER_IDS:
        blockers.append("trusted_status_requires_explicit_human_reviewer_id")
    if not (source_decision_packet or "").strip():
        blockers.append("trusted_status_requires_source_decision_packet")
    if not (rationale or "").strip():
        blockers.append("trusted_status_requires_rationale")
    packet_blockers = {
        "packet_missing_blocker": "trusted_status_requires_source_decision_packet",
        "packet_backed_blocker": "trusted_status_requires_packet_backed_source_decision_packet",
        "hash_missing_blocker": "trusted_status_requires_source_artifact_hash",
        "hash_format_blocker": "trusted_status_requires_sha256_source_artifact_hash",
        "hash_mismatch_blocker": "trusted_status_requires_matching_source_artifact_hash",
        "packet_content_blocker": "trusted_status_requires_packet_row_for_decision",
        "packet_audit_blocker": "trusted_status_requires_audited_human_decision_artifact",
    }
    expected_reference_tokens = manual_preprocess_reference_tokens(kind, item_id)
    if kind == "ksa":
        packet_blockers = {
            "packet_missing_blocker": "ksa_manual_preprocess_requires_source_decision_packet",
            "packet_backed_blocker": "ksa_manual_preprocess_requires_packet_backed_source_decision_packet",
            "hash_missing_blocker": "ksa_manual_preprocess_requires_source_artifact_hash",
            "hash_format_blocker": "ksa_manual_preprocess_requires_sha256_source_artifact_hash",
            "hash_mismatch_blocker": "ksa_manual_preprocess_requires_matching_source_artifact_hash",
            "packet_content_blocker": "ksa_manual_preprocess_requires_packet_row_for_ksa_decision",
            "packet_audit_blocker": "ksa_manual_preprocess_requires_audited_human_decision_artifact",
        }
    for blocker in trusted_review_packet_blockers(
        source_decision_packet=source_decision_packet,
        source_artifact_hash=source_artifact_hash,
        expected_reference_tokens=expected_reference_tokens,
        reviewer_id=reviewer_id,
        **packet_blockers,
    ):
        if blocker not in blockers:
            blockers.append(blocker)
    return blockers


def save_manual_preprocess(db_path: Path, payload: dict) -> dict:
    kind = str(payload["kind"])
    item_id = str(payload["id"])
    title_refined = str(payload.get("title_refined", "")).strip()
    body_refined = str(payload.get("body_refined", "")).strip()
    reviewer_id = str(payload.get("reviewer_id", "dashboard")).strip() or "dashboard"
    notes = str(payload.get("notes", "")).strip()
    source_decision_packet = str(payload.get("source_decision_packet", "")).strip() or None
    source_artifact_hash = str(payload.get("source_artifact_hash", "")).strip() or None
    rationale = str(payload.get("rationale", "")).strip() or notes or None
    run_artifact = str(payload.get("run_artifact", "")).strip() or None
    evidence_refs_payload = payload.get("evidence_refs")
    if isinstance(evidence_refs_payload, str):
        evidence_refs = [ref.strip() for ref in evidence_refs_payload.splitlines() if ref.strip()]
    elif isinstance(evidence_refs_payload, list):
        evidence_refs = [str(ref).strip() for ref in evidence_refs_payload if str(ref).strip()]
    else:
        evidence_refs = []
    provenance_blockers = manual_preprocess_provenance_blockers(
        kind=kind,
        item_id=item_id,
        reviewer_id=reviewer_id,
        source_decision_packet=source_decision_packet,
        source_artifact_hash=source_artifact_hash,
        rationale=rationale,
    )
    if provenance_blockers:
        return {
            "ok": False,
            "error": "trusted_status_requires_provenance",
            "blockers": provenance_blockers,
            "kind": kind,
            "id": item_id,
        }
    conn = connect_db(db_path, prepare_ontology=(kind == "ksa"))
    previous_status: str | None = None
    audit_entity_type = kind
    if kind == "classification":
        row = conn.execute(
            "SELECT review_status FROM classifications WHERE classification_id = ?",
            (item_id,),
        ).fetchone()
        previous_status = row["review_status"] if row is not None else None
        audit_entity_type = "classification"
        conn.execute(
            """
            UPDATE classifications
            SET duty_def_refined = ?, review_status = 'human_reviewed'
            WHERE classification_id = ?
            """,
            (body_refined, item_id),
        )
    elif kind == "unit":
        row = conn.execute(
            "SELECT review_status FROM competency_units WHERE unit_code = ?",
            (item_id,),
        ).fetchone()
        previous_status = row["review_status"] if row is not None else None
        audit_entity_type = "competency_unit"
        conn.execute(
            """
            UPDATE competency_units
            SET unit_name_refined = ?, api_definition_refined = ?, review_status = 'human_reviewed', updated_at = ?
            WHERE unit_code = ?
            """,
            (title_refined, body_refined, now_utc(), item_id),
        )
    elif kind == "element":
        row = conn.execute(
            "SELECT review_status FROM competency_elements WHERE element_id = ?",
            (item_id,),
        ).fetchone()
        previous_status = row["review_status"] if row is not None else None
        audit_entity_type = "competency_element"
        conn.execute(
            """
            UPDATE competency_elements
            SET element_name_refined = ?, review_status = 'human_reviewed'
            WHERE element_id = ?
            """,
            (title_refined, item_id),
        )
    elif kind == "criteria":
        row = conn.execute(
            "SELECT review_status FROM performance_criteria WHERE criteria_id = ?",
            (item_id,),
        ).fetchone()
        previous_status = row["review_status"] if row is not None else None
        audit_entity_type = "performance_criteria"
        conn.execute(
            """
            UPDATE performance_criteria
            SET criteria_text_refined = ?, review_status = 'human_reviewed'
            WHERE criteria_id = ?
            """,
            (body_refined, item_id),
        )
    elif kind == "ksa":
        row = conn.execute(
            """
            SELECT ki.ksa_id, ki.ksa_type_name, ki.ksa_text_raw, ki.review_status, kcl.concept_id
            FROM ksa_items ki
            LEFT JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
            WHERE ki.ksa_id = ?
            """,
            (item_id,),
        ).fetchone()
        if row is None:
            conn.close()
            raise ValueError(f"unknown ksa id: {item_id}")
        previous_status = row["review_status"]
        audit_entity_type = "ksa_item"
        concept_type = {"지식": "knowledge", "기술": "skill", "태도": "attitude"}.get(
            row["ksa_type_name"],
            "knowledge",
        )
        concept_name = title_refined or row["ksa_text_raw"]
        concept_id = get_or_create_concept(conn, concept_type, concept_name)
        timestamp = now_utc()
        conn.execute(
            """
            UPDATE ontology_concepts
            SET concept_name = ?,
                normalized_key = ?,
                definition = ?,
                definition_status = ?,
                review_status = 'human_reviewed',
                updated_at = ?
            WHERE concept_id = ?
            """,
            (
                concept_name,
                normalize_concept_key(concept_name),
                body_refined or None,
                "defined" if body_refined else "missing",
                timestamp,
                concept_id,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
            VALUES (?, ?, 'human_reviewed', ?)
            """,
            (item_id, concept_id, timestamp),
        )
        conn.execute(
            """
            UPDATE ksa_concept_links
            SET concept_id = ?, link_status = 'human_reviewed'
            WHERE ksa_id = ?
            """,
            (concept_id, item_id),
        )
        conn.execute(
            """
            UPDATE ksa_items
            SET ksa_text_refined = ?, review_status = 'human_reviewed'
            WHERE ksa_id = ?
            """,
            (concept_name, item_id),
        )
        aliases = split_lines(payload.get("aliases"))
        aliases.append(row["ksa_text_raw"])
        save_concept_aliases(conn, concept_id, aliases)
        replace_concept_relations(
            conn,
            source_concept_id=concept_id,
            concept_type=concept_type,
            relation_type="parent",
            target_names=split_lines(payload.get("parent_concepts")),
        )
        replace_concept_relations(
            conn,
            source_concept_id=concept_id,
            concept_type=concept_type,
            relation_type="child",
            target_names=split_lines(payload.get("child_concepts")),
        )
        replace_concept_relations(
            conn,
            source_concept_id=concept_id,
            concept_type=concept_type,
            relation_type="related",
            target_names=split_lines(payload.get("related_concepts")),
        )
        relation_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM ontology_concept_relations WHERE source_concept_id = ?",
                (concept_id,),
            ).fetchone()[0]
        )
        conn.execute(
            """
            UPDATE ontology_concepts
            SET relation_status = ?, updated_at = ?
            WHERE concept_id = ?
            """,
            ("linked" if relation_count else "unlinked", timestamp, concept_id),
        )
    else:
        conn.close()
        raise ValueError(f"unsupported kind: {kind}")
    issue_id = payload.get("issue_id")
    if issue_id:
        conn.execute("UPDATE quality_issues SET resolved_at = ? WHERE issue_id = ?", (now_utc(), issue_id))
    if previous_status is not None:
        insert_review_audit(
            conn,
            entity_type=audit_entity_type,
            entity_id=item_id,
            action="save_manual_preprocess",
            previous_status=previous_status,
            new_status="human_reviewed",
            reviewer_id=reviewer_id,
            notes=notes,
            source_decision_packet=source_decision_packet,
            source_artifact_hash=source_artifact_hash,
            rationale=rationale,
            evidence_refs=evidence_refs,
            created_by_tool="ncs_dashboard.save_manual_preprocess",
            run_artifact=run_artifact,
        )
    conn.commit()
    conn.close()
    return {"ok": True, "kind": kind, "id": item_id}


def insert_review_audit(
    conn,
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    previous_status: str | None,
    new_status: str | None,
    reviewer_id: str | None,
    notes: str | None,
    source_decision_packet: str | None = None,
    source_artifact_hash: str | None = None,
    rationale: str | None = None,
    evidence_refs: list[str] | None = None,
    created_by_tool: str | None = None,
    run_artifact: str | None = None,
) -> None:
    evidence_refs_json = json.dumps(evidence_refs or [], ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO review_audit_log(
            entity_type, entity_id, action, previous_status,
            new_status, reviewer_id, notes, source_decision_packet,
            source_artifact_hash, rationale, evidence_refs_json,
            created_by_tool, run_artifact, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_type,
            entity_id,
            action,
            previous_status,
            new_status,
            reviewer_id,
            notes,
            source_decision_packet,
            source_artifact_hash,
            rationale,
            evidence_refs_json,
            created_by_tool,
            run_artifact,
            now_utc(),
        ),
    )


def _ksa_label_status_override_blockers(payload: dict, *, prefix: str) -> list[str]:
    blockers: list[str] = []
    for field in KSA_LABEL_FORBIDDEN_STATUS_OVERRIDE_FIELDS:
        if field in payload and str(payload.get(field) or "").strip():
            blockers.append(f"{prefix}_rejects_status_override_{field}")
    for field in KSA_LABEL_FORBIDDEN_APPROVAL_PAYLOAD_FIELDS:
        if field not in payload:
            continue
        raw = payload.get(field)
        normalized = str(raw).strip().lower()
        if raw is True or normalized not in {"", "0", "false", "none", "null"}:
            blockers.append(f"{prefix}_rejects_approval_claim_{field}")
    return blockers


def review_ksa_label_candidate(db_path: Path, payload: dict) -> dict:
    try:
        label_id = int(payload.get("label_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_label_id"}
    decision = str(payload.get("decision", "")).strip()
    allowed = {
        "approve": "human_reviewed",
        "needs_revision": "needs_review",
        "reject": "rejected",
    }
    if decision not in allowed:
        return {
            "ok": False,
            "error": "unsupported_ksa_label_review_decision",
            "allowed_decisions": sorted(allowed),
        }

    reviewer_id = str(payload.get("reviewer_id", "")).strip()
    notes = str(payload.get("notes", "")).strip()
    rationale = str(payload.get("rationale", "")).strip() or notes
    raw_to_label_checked = payload.get("raw_to_label_checked") is True
    source_decision_packet = str(payload.get("source_decision_packet", "")).strip() or None
    source_artifact_hash = str(payload.get("source_artifact_hash", "")).strip() or None
    run_artifact = str(payload.get("run_artifact", "")).strip() or None
    blockers: list[str] = []
    blockers.extend(_ksa_label_status_override_blockers(payload, prefix="label_review"))
    if reviewer_id.lower() in DASHBOARD_AUTOMATED_REVIEWER_IDS:
        blockers.append("label_review_requires_explicit_human_reviewer_id")
    if not notes:
        blockers.append("label_review_requires_human_note")
    if not rationale:
        blockers.append("label_review_requires_rationale")
    if decision == "approve":
        blockers.extend(
            trusted_review_packet_blockers(
                source_decision_packet=source_decision_packet,
                source_artifact_hash=source_artifact_hash,
                packet_missing_blocker="label_review_approve_requires_source_decision_packet",
                packet_backed_blocker=(
                    "label_review_approve_requires_packet_backed_source_decision_packet"
                ),
                hash_missing_blocker="label_review_approve_requires_source_artifact_hash",
                hash_format_blocker="label_review_approve_requires_sha256_source_artifact_hash",
                hash_mismatch_blocker=(
                    "label_review_approve_requires_matching_source_artifact_hash"
                ),
                packet_content_blocker=(
                    "label_review_approve_requires_packet_row_for_label_decision"
                ),
                expected_reference_tokens=(f"label:{label_id}", decision),
                reviewer_id=reviewer_id,
                packet_audit_blocker="label_review_approve_requires_audited_human_decision_artifact",
            )
        )
    if decision == "approve" and not run_artifact:
        blockers.append("label_review_approve_requires_run_artifact")
    if not raw_to_label_checked:
        blockers.append("label_review_requires_raw_to_label_check")
    if blockers:
        return {
            "ok": False,
            "error": "ksa_label_review_requires_human_input",
            "blockers": blockers,
            "label_id": label_id,
        }

    conn = connect_db(db_path, prepare_ontology=True)
    row = conn.execute(
        """
        SELECT
          label.label_id,
          label.concept_id,
          label.source_ksa_id,
          label.source_atomic_id,
          label.source_scope_key,
          label.concept_type,
          label.source_text,
          label.label_text,
          label.source_method,
          label.review_status,
          oc.concept_name,
          ki.ksa_text_raw,
          atom.atom_text
        FROM ontology_concept_label_candidates label
        LEFT JOIN ontology_concepts oc ON oc.concept_id = label.concept_id
        LEFT JOIN ksa_items ki ON ki.ksa_id = label.source_ksa_id
        LEFT JOIN ksa_atomic_items atom ON atom.atomic_id = label.source_atomic_id
        WHERE label.label_id = ?
        """,
        (label_id,),
    ).fetchone()
    if row is None:
        conn.close()
        return {"ok": False, "error": "not_found", "label_id": label_id}

    previous = str(row["review_status"] or "")
    new_status = allowed[decision]
    timestamp = now_utc()
    conn.execute(
        """
        UPDATE ontology_concept_label_candidates
        SET review_status = ?, updated_at = ?
        WHERE label_id = ?
        """,
        (new_status, timestamp, label_id),
    )
    evidence_refs = [
        f"label_id:{row['label_id']}",
        f"concept_id:{row['concept_id']}",
        f"source_ksa_id:{row['source_ksa_id']}",
        f"source_atomic_id:{row['source_atomic_id']}",
        f"source_scope_key:{row['source_scope_key']}",
        f"concept_name:{row['concept_name'] or ''}",
        f"source_text:{row['source_text'] or ''}",
        f"label_text:{row['label_text'] or ''}",
        "raw_to_label_checked:true",
    ]
    insert_review_audit(
        conn,
        entity_type="ontology_concept_label_candidate",
        entity_id=str(label_id),
        action=f"ksa_label_{decision}",
        previous_status=previous,
        new_status=new_status,
        reviewer_id=reviewer_id,
        notes=notes,
        source_decision_packet=source_decision_packet,
        source_artifact_hash=source_artifact_hash,
        rationale=rationale,
        evidence_refs=evidence_refs,
        created_by_tool="ncs_dashboard.review_ksa_label_candidate",
        run_artifact=run_artifact,
    )
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "label_id": label_id,
        "decision": decision,
        "previous_status": previous,
        "new_status": new_status,
        "audit_logged": True,
        "raw_ksa_preserved": True,
        "concept_name_preserved": True,
        "raw_to_label_checked": True,
    }


def edit_ksa_label_candidate(db_path: Path, payload: dict) -> dict:
    try:
        label_id = int(payload.get("label_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_label_id"}

    corrected_label = re.sub(r"\s+", " ", str(payload.get("corrected_label_text", "")).strip())
    reviewer_id = str(payload.get("reviewer_id", "")).strip()
    notes = str(payload.get("notes", "")).strip()
    rationale = str(payload.get("rationale", "")).strip() or notes
    raw_to_label_checked = payload.get("raw_to_label_checked") is True
    source_decision_packet = str(payload.get("source_decision_packet", "")).strip() or None
    source_artifact_hash = str(payload.get("source_artifact_hash", "")).strip() or None
    run_artifact = str(payload.get("run_artifact", "")).strip() or None

    blockers: list[str] = []
    blockers.extend(_ksa_label_status_override_blockers(payload, prefix="label_edit"))
    if reviewer_id.lower() in DASHBOARD_AUTOMATED_REVIEWER_IDS:
        blockers.append("label_edit_requires_explicit_human_reviewer_id")
    if not corrected_label:
        blockers.append("label_edit_requires_corrected_label_text")
    if len(corrected_label) > 60:
        blockers.append("label_edit_corrected_label_too_long")
    if not notes:
        blockers.append("label_edit_requires_human_note")
    if not rationale:
        blockers.append("label_edit_requires_rationale")
    blockers.extend(
        trusted_review_packet_blockers(
            source_decision_packet=source_decision_packet,
            source_artifact_hash=source_artifact_hash,
            packet_missing_blocker="label_edit_requires_source_decision_packet",
            packet_backed_blocker="label_edit_requires_packet_backed_source_decision_packet",
            hash_missing_blocker="label_edit_requires_source_artifact_hash",
            hash_format_blocker="label_edit_requires_sha256_source_artifact_hash",
            hash_mismatch_blocker="label_edit_requires_matching_source_artifact_hash",
            packet_content_blocker="label_edit_requires_packet_row_for_label_decision",
            expected_reference_tokens=(f"label:{label_id}", "edit_approve"),
            reviewer_id=reviewer_id,
            packet_audit_blocker="label_edit_requires_audited_human_decision_artifact",
        )
    )
    if not run_artifact:
        blockers.append("label_edit_requires_run_artifact")
    if not raw_to_label_checked:
        blockers.append("label_edit_requires_raw_to_label_check")
    if blockers:
        return {
            "ok": False,
            "error": "ksa_label_edit_requires_human_input",
            "blockers": blockers,
            "label_id": label_id,
        }

    normalized_key = normalize_concept_key(corrected_label)
    if not normalized_key:
        return {
            "ok": False,
            "error": "label_edit_corrected_label_normalizes_empty",
            "label_id": label_id,
        }

    conn = connect_db(db_path, prepare_ontology=True)
    row = conn.execute(
        """
        SELECT
          label.label_id,
          label.concept_id,
          label.source_ksa_id,
          label.source_atomic_id,
          label.source_scope_key,
          label.concept_type,
          label.source_text,
          label.label_text,
          label.normalized_label_key,
          label.source_method,
          label.review_status,
          oc.concept_name
        FROM ontology_concept_label_candidates label
        LEFT JOIN ontology_concepts oc ON oc.concept_id = label.concept_id
        WHERE label.label_id = ?
        """,
        (label_id,),
    ).fetchone()
    if row is None:
        conn.close()
        return {"ok": False, "error": "not_found", "label_id": label_id}

    previous_status = str(row["review_status"] or "")
    previous_label = str(row["label_text"] or "")
    quality_flags = ksa_label_quality_flags(
        str(row["source_text"] or ""),
        corrected_label,
        str(row["concept_type"] or ""),
    )
    timestamp = now_utc()
    try:
        conn.execute(
            """
            UPDATE ontology_concept_label_candidates
            SET label_text = ?,
                normalized_label_key = ?,
                review_status = 'human_reviewed',
                evidence_text = COALESCE(evidence_text, '') || ?,
                updated_at = ?
            WHERE label_id = ?
            """,
            (
                corrected_label,
                normalized_key,
                f" | human_corrected_label: {corrected_label}",
                timestamp,
                label_id,
            ),
        )
    except sqlite3.IntegrityError as exc:
        conn.close()
        return {
            "ok": False,
            "error": "label_edit_conflicts_with_existing_candidate",
            "detail": str(exc),
            "label_id": label_id,
        }

    evidence_refs = [
        f"label_id:{row['label_id']}",
        f"concept_id:{row['concept_id']}",
        f"source_ksa_id:{row['source_ksa_id']}",
        f"source_atomic_id:{row['source_atomic_id']}",
        f"source_scope_key:{row['source_scope_key']}",
        f"concept_name:{row['concept_name'] or ''}",
        f"source_text:{row['source_text'] or ''}",
        f"previous_label_text:{previous_label}",
        f"corrected_label_text:{corrected_label}",
        "raw_to_label_checked:true",
    ]
    insert_review_audit(
        conn,
        entity_type="ontology_concept_label_candidate",
        entity_id=str(label_id),
        action="ksa_label_edit_approve",
        previous_status=previous_status,
        new_status="human_reviewed",
        reviewer_id=reviewer_id,
        notes=notes,
        source_decision_packet=source_decision_packet,
        source_artifact_hash=source_artifact_hash,
        rationale=rationale,
        evidence_refs=evidence_refs,
        created_by_tool="ncs_dashboard.edit_ksa_label_candidate",
        run_artifact=run_artifact,
    )
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "label_id": label_id,
        "previous_label_text": previous_label,
        "corrected_label_text": corrected_label,
        "previous_status": previous_status,
        "new_status": "human_reviewed",
        "quality_flags": quality_flags,
        "audit_logged": True,
        "raw_ksa_preserved": True,
    }


def review_ksa_meaning_candidate(db_path: Path, payload: dict) -> dict:
    try:
        meaning_id = int(payload.get("meaning_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_meaning_id"}
    decision = str(payload.get("decision", "")).strip()
    allowed = {
        "approve": "human_reviewed",
        "needs_revision": "needs_review",
        "reject": "rejected",
    }
    if decision not in allowed:
        return {
            "ok": False,
            "error": "unsupported_ksa_meaning_review_decision",
            "allowed_decisions": sorted(allowed),
        }

    reviewer_id = str(payload.get("reviewer_id", "")).strip()
    notes = str(payload.get("notes", "")).strip()
    rationale = str(payload.get("rationale", "")).strip() or notes
    raw_to_meaning_checked = payload.get("raw_to_meaning_checked") is True
    source_decision_packet = str(payload.get("source_decision_packet", "")).strip() or None
    source_artifact_hash = str(payload.get("source_artifact_hash", "")).strip() or None
    run_artifact = str(payload.get("run_artifact", "")).strip() or None
    blockers: list[str] = []
    if reviewer_id.lower() in DASHBOARD_AUTOMATED_REVIEWER_IDS:
        blockers.append("meaning_review_requires_explicit_human_reviewer_id")
    if not notes:
        blockers.append("meaning_review_requires_human_note")
    if not rationale:
        blockers.append("meaning_review_requires_rationale")
    if decision == "approve":
        blockers.extend(
            trusted_review_packet_blockers(
                source_decision_packet=source_decision_packet,
                source_artifact_hash=source_artifact_hash,
                packet_missing_blocker="meaning_review_approve_requires_source_decision_packet",
                packet_backed_blocker=(
                    "meaning_review_approve_requires_packet_backed_source_decision_packet"
                ),
                hash_missing_blocker="meaning_review_approve_requires_source_artifact_hash",
                hash_format_blocker=(
                    "meaning_review_approve_requires_sha256_source_artifact_hash"
                ),
                hash_mismatch_blocker=(
                    "meaning_review_approve_requires_matching_source_artifact_hash"
                ),
                packet_content_blocker=(
                    "meaning_review_approve_requires_packet_row_for_meaning_decision"
                ),
                expected_reference_tokens=(f"meaning:{meaning_id}", decision),
                reviewer_id=reviewer_id,
                packet_audit_blocker="meaning_review_approve_requires_audited_human_decision_artifact",
            )
        )
    if decision == "approve" and not run_artifact:
        blockers.append("meaning_review_approve_requires_run_artifact")
    if not raw_to_meaning_checked:
        blockers.append("meaning_review_requires_raw_to_meaning_check")
    if blockers:
        return {
            "ok": False,
            "error": "ksa_meaning_review_requires_human_input",
            "blockers": blockers,
            "meaning_id": meaning_id,
        }

    conn = connect_db(db_path, prepare_ontology=True)
    row = conn.execute(
        """
        SELECT
          kmc.meaning_id,
          kmc.concept_id,
          kmc.concept_type,
          kmc.meaning_role,
          kmc.meaning_text,
          kmc.source_method,
          kmc.evidence_text,
          kmc.unit_code,
          kmc.element_id,
          kmc.criteria_id,
          kmc.ksa_id,
          kmc.confidence_score,
          kmc.review_status,
          oc.concept_name,
          oc.definition_status,
          oc.review_status AS concept_review_status,
          ki.ksa_text_raw,
          cu.unit_name_raw,
          ce.element_name_raw,
          pc.criteria_text_raw
        FROM ksa_meaning_candidates kmc
        LEFT JOIN ontology_concepts oc ON oc.concept_id = kmc.concept_id
        LEFT JOIN ksa_items ki ON ki.ksa_id = kmc.ksa_id
        LEFT JOIN competency_units cu ON cu.unit_code = kmc.unit_code
        LEFT JOIN competency_elements ce ON ce.element_id = kmc.element_id
        LEFT JOIN performance_criteria pc ON pc.criteria_id = kmc.criteria_id
        WHERE kmc.meaning_id = ?
        """,
        (meaning_id,),
    ).fetchone()
    if row is None:
        conn.close()
        return {"ok": False, "error": "not_found", "meaning_id": meaning_id}

    if decision == "approve" and row["ksa_id"] is None:
        conn.close()
        return {
            "ok": False,
            "error": "ksa_meaning_review_requires_scoped_ksa_for_approval",
            "blockers": ["meaning_review_approve_requires_scoped_ksa"],
            "meaning_id": meaning_id,
        }
    if decision == "approve" and not (row["criteria_id"] or row["element_id"] or row["unit_code"]):
        conn.close()
        return {
            "ok": False,
            "error": "ksa_meaning_review_requires_task_scope_for_approval",
            "blockers": ["meaning_review_approve_requires_task_scope"],
            "meaning_id": meaning_id,
        }
    if decision == "approve" and not (row["criteria_text_raw"] or row["evidence_text"]):
        conn.close()
        return {
            "ok": False,
            "error": "ksa_meaning_review_requires_visible_task_evidence_for_approval",
            "blockers": ["meaning_review_approve_requires_visible_task_evidence"],
            "meaning_id": meaning_id,
        }

    previous = str(row["review_status"] or "")
    new_status = allowed[decision]
    timestamp = now_utc()
    conn.execute(
        """
        UPDATE ksa_meaning_candidates
        SET review_status = ?, updated_at = ?
        WHERE meaning_id = ?
        """,
        (new_status, timestamp, meaning_id),
    )
    evidence_refs = [
        f"meaning_id:{row['meaning_id']}",
        f"concept_id:{row['concept_id']}",
        f"concept_name:{row['concept_name'] or ''}",
        f"meaning_role:{row['meaning_role'] or ''}",
        f"source_method:{row['source_method'] or ''}",
        f"unit_code:{row['unit_code'] or ''}",
        f"element_id:{row['element_id']}",
        f"criteria_id:{row['criteria_id']}",
        f"ksa_id:{row['ksa_id']}",
        f"ksa_text_raw:{row['ksa_text_raw'] or ''}",
        f"meaning_text:{row['meaning_text'] or ''}",
        f"evidence_text:{row['evidence_text'] or ''}",
        "raw_to_meaning_checked:true",
    ]
    insert_review_audit(
        conn,
        entity_type="ksa_meaning_candidate",
        entity_id=str(meaning_id),
        action=f"ksa_meaning_{decision}",
        previous_status=previous,
        new_status=new_status,
        reviewer_id=reviewer_id,
        notes=notes,
        source_decision_packet=source_decision_packet,
        source_artifact_hash=source_artifact_hash,
        rationale=rationale,
        evidence_refs=evidence_refs,
        created_by_tool="ncs_dashboard.review_ksa_meaning_candidate",
        run_artifact=run_artifact,
    )
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "meaning_id": meaning_id,
        "decision": decision,
        "previous_status": previous,
        "new_status": new_status,
        "audit_logged": True,
        "raw_ksa_preserved": True,
        "concept_definition_status_preserved": True,
        "raw_to_meaning_checked": True,
    }


def review_mapping_candidate(db_path: Path, payload: dict) -> dict:
    match_id = str(payload.get("match_id", "")).strip()
    action = str(payload.get("action", "")).strip()
    reviewer_id = str(payload.get("reviewer_id", "")).strip()
    notes = str(payload.get("notes", "")).strip()
    relation = str(payload.get("relation", "")).strip()
    source_decision_packet = str(payload.get("source_decision_packet", "")).strip() or None
    source_artifact_hash = str(payload.get("source_artifact_hash", "")).strip() or None
    rationale = str(payload.get("rationale", "")).strip() or notes
    run_artifact = str(payload.get("run_artifact", "")).strip() or None
    evidence_refs_payload = payload.get("evidence_refs")
    evidence_refs = [
        str(ref).strip()
        for ref in evidence_refs_payload
        if str(ref).strip()
    ] if isinstance(evidence_refs_payload, list) else []
    allowed = {
        "accept": "accepted",
        "reject": "rejected",
        "mark_low_confidence": "low_confidence",
        "revise_relation": "revised",
    }
    if not match_id:
        return {"ok": False, "error": "invalid_match_id"}
    if action not in allowed:
        return {
            "ok": False,
            "error": "unsupported_mapping_review_action",
            "allowed_actions": sorted(allowed),
        }
    blockers: list[str] = []
    if reviewer_id.lower() in DASHBOARD_AUTOMATED_REVIEWER_IDS:
        blockers.append("mapping_review_requires_explicit_human_reviewer_id")
    if not notes:
        blockers.append("mapping_review_requires_human_note")
    if not rationale:
        blockers.append("mapping_review_requires_rationale")
    if not source_decision_packet:
        blockers.append("mapping_review_requires_source_decision_packet")
    if not evidence_refs:
        blockers.append("mapping_review_requires_evidence_refs")
    for blocker in trusted_review_packet_blockers(
        source_decision_packet=source_decision_packet,
        source_artifact_hash=source_artifact_hash,
        packet_missing_blocker="mapping_review_requires_source_decision_packet",
        packet_backed_blocker="mapping_review_requires_packet_backed_source_decision_packet",
        hash_missing_blocker="mapping_review_requires_source_artifact_hash",
        hash_format_blocker="mapping_review_requires_sha256_source_artifact_hash",
        hash_mismatch_blocker="mapping_review_requires_matching_source_artifact_hash",
        packet_content_blocker="mapping_review_requires_packet_row_for_match_decision",
        expected_reference_tokens=(f"match_id:{match_id}",),
        reviewer_id=reviewer_id,
        packet_audit_blocker="mapping_review_requires_audited_human_decision_artifact",
    ):
        if blocker not in blockers:
            blockers.append(blocker)
    if blockers:
        return {
            "ok": False,
            "error": "sqf_mapping_review_requires_human_decision_packet",
            "blockers": blockers,
            "match_id": match_id,
            "status_update_allowed": False,
        }
    conn = connect_db(db_path)
    row = conn.execute("SELECT * FROM sqf_ncs_matches WHERE match_id = ?", (match_id,)).fetchone()
    if row is None:
        conn.close()
        return {"ok": False, "error": "not_found", "match_id": match_id}
    previous = row["review_status"]
    new_status = allowed[action]
    timestamp = now_utc()
    updates = [
        "review_status = ?",
        "reviewer_id = ?",
        "reviewed_at = ?",
        "reviewer_notes = ?",
        "updated_at = ?",
    ]
    values = [new_status, reviewer_id, timestamp, notes, timestamp]
    if action == "revise_relation":
        if not relation:
            conn.close()
            return {
                "ok": False,
                "error": "relation_required_for_revise_relation",
                "match_id": match_id,
            }
        updates.append("relation = ?")
        values.append(relation)
    values.append(match_id)
    conn.execute(f"UPDATE sqf_ncs_matches SET {', '.join(updates)} WHERE match_id = ?", values)
    insert_review_audit(
        conn,
        entity_type="sqf_ncs_match",
        entity_id=match_id,
        action=action,
        previous_status=previous,
        new_status=new_status,
        reviewer_id=reviewer_id,
        notes=notes,
        source_decision_packet=source_decision_packet,
        source_artifact_hash=source_artifact_hash,
        rationale=rationale,
        evidence_refs=evidence_refs,
        created_by_tool="ncs_dashboard.review_mapping_candidate",
        run_artifact=run_artifact,
    )
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "match_id": match_id,
        "previous_status": previous,
        "new_status": new_status,
        "audit_logged": True,
        "status_update_allowed": True,
    }


def review_refinement_job(db_path: Path, payload: dict) -> dict:
    job_id = str(payload["job_id"])
    action = str(payload["action"])
    reviewer_id = str(payload.get("reviewer_id", "dashboard")).strip() or "dashboard"
    notes = str(payload.get("notes", "")).strip()
    source_decision_packet = str(payload.get("source_decision_packet", "")).strip() or None
    source_artifact_hash = str(payload.get("source_artifact_hash", "")).strip() or None
    rationale = str(payload.get("rationale", "")).strip() or notes or None
    run_artifact = str(payload.get("run_artifact", "")).strip() or None
    evidence_refs_payload = payload.get("evidence_refs")
    if isinstance(evidence_refs_payload, str):
        evidence_refs = [ref.strip() for ref in evidence_refs_payload.splitlines() if ref.strip()]
    elif isinstance(evidence_refs_payload, list):
        evidence_refs = [str(ref).strip() for ref in evidence_refs_payload if str(ref).strip()]
    else:
        evidence_refs = []
    if action not in {"approve_refined", "reject_refined", "edit_refined"}:
        raise ValueError(f"unsupported refinement review action: {action}")
    conn = connect_db(db_path)
    row = conn.execute("SELECT * FROM refinement_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        conn.close()
        return {"error": "not_found", "job_id": job_id}
    provenance_blockers: list[str] = []
    if action in {"approve_refined", "edit_refined"}:
        provenance_blockers.extend(
            manual_preprocess_provenance_blockers(
                kind="refinement",
                item_id=str(job_id),
                reviewer_id=reviewer_id,
                source_decision_packet=source_decision_packet,
                source_artifact_hash=source_artifact_hash,
                rationale=rationale,
            )
        )
        if not notes:
            provenance_blockers.append("refinement_review_requires_human_note")
        if not source_artifact_hash:
            provenance_blockers.append("refinement_review_requires_source_artifact_hash")
        elif not source_artifact_hash.startswith("sha256:"):
            provenance_blockers.append("refinement_review_requires_sha256_source_artifact_hash")
        if not run_artifact:
            provenance_blockers.append("refinement_review_requires_run_artifact")
        for blocker in trusted_review_packet_blockers(
            source_decision_packet=source_decision_packet,
            source_artifact_hash=source_artifact_hash,
            packet_missing_blocker="refinement_review_requires_source_decision_packet",
            packet_backed_blocker="refinement_review_requires_packet_backed_source_decision_packet",
            hash_missing_blocker="refinement_review_requires_source_artifact_hash",
            hash_format_blocker="refinement_review_requires_sha256_source_artifact_hash",
            hash_mismatch_blocker="refinement_review_requires_matching_source_artifact_hash",
            packet_content_blocker=refinement_target_packet_content_blocker(str(row["target_type"])),
            expected_reference_tokens=manual_preprocess_reference_tokens(str(row["target_type"]), str(row["target_id"])),
            reviewer_id=reviewer_id,
            packet_audit_blocker="refinement_review_requires_audited_human_decision_artifact",
        ):
            if blocker not in provenance_blockers:
                provenance_blockers.append(blocker)
        if provenance_blockers:
            conn.close()
            return {
                "ok": False,
                "error": "refinement_review_requires_human_decision_packet",
                "blockers": provenance_blockers,
                "job_id": job_id,
                "status_update_allowed": False,
            }
    previous = row["review_status"]
    if action == "reject_refined":
        new_status = "rejected"
        conn.execute("UPDATE refinement_jobs SET review_status = ? WHERE job_id = ?", (new_status, job_id))
    else:
        refined_text = str(payload.get("refined_text") or row["refined_text"] or "").strip()
        if not refined_text:
            conn.close()
            raise ValueError("refined_text is required")
        apply_refinement_to_target(
            conn,
            target_type=row["target_type"],
            target_id=row["target_id"],
            refined_text=refined_text,
            review_status="human_reviewed",
        )
        new_status = "applied"
        conn.execute(
            """
            UPDATE refinement_jobs
            SET refined_text = ?, rationale = ?, review_status = ?, applied_at = ?
            WHERE job_id = ?
            """,
            (refined_text, rationale or row["rationale"], new_status, now_utc(), job_id),
        )
    if not evidence_refs:
        evidence_refs = [
            f"refinement_job:{job_id}",
            f"target_type:{row['target_type']}",
            f"target_id:{row['target_id']}",
            f"source_issue_id:{row['source_issue_id']}",
            f"model_name:{row['model_name']}",
            f"prompt_version:{row['prompt_version']}",
        ]
    insert_review_audit(
        conn,
        entity_type="refinement_job",
        entity_id=job_id,
        action=action,
        previous_status=previous,
        new_status=new_status,
        reviewer_id=reviewer_id,
        notes=notes,
        source_decision_packet=source_decision_packet,
        source_artifact_hash=source_artifact_hash,
        rationale=rationale,
        evidence_refs=evidence_refs,
        created_by_tool="ncs_dashboard.review_refinement_job",
        run_artifact=run_artifact,
    )
    conn.commit()
    conn.close()
    return {
        "ok": True,
        "job_id": job_id,
        "previous_status": previous,
        "new_status": new_status,
        "audit_logged": True,
        "status_update_allowed": True,
        "automatic_status_update_allowed": False,
        "human_decision_packet_required": action in {"approve_refined", "edit_refined"},
    }


def get_classifications(db_path: Path, params: dict[str, list[str]]) -> dict:
    clauses, values = classification_filters(params, "c")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = safe_limit(params)
    conn = connect_db_for_read(db_path, required_tables=DASHBOARD_LOOKUP_TABLES)
    rows = conn.execute(
        f"""
        SELECT
            c.*,
            COUNT(DISTINCT cu.unit_code) AS unit_count,
            COUNT(DISTINCT ce.element_id) AS element_count,
            COUNT(DISTINCT pc.criteria_id) AS criteria_count,
            COUNT(DISTINCT ki.ksa_id) AS ksa_count
        FROM classifications c
        LEFT JOIN competency_units cu ON cu.classification_id = c.classification_id
        LEFT JOIN competency_elements ce ON ce.unit_code = cu.unit_code
        LEFT JOIN performance_criteria pc ON pc.element_id = ce.element_id
        LEFT JOIN ksa_items ki ON ki.element_id = ce.element_id
        {where}
        GROUP BY c.classification_id
        ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code
        LIMIT ?
        """,
        [*values, limit],
    ).fetchall()
    conn.close()
    return {"classifications": [dict(row) for row in rows]}


def get_units(db_path: Path, params: dict[str, list[str]]) -> dict:
    clauses, values = classification_filters(params, "c")
    keyword = first(params, "keyword")
    status = first(params, "api_match_status")
    if keyword:
        clauses.append("(cu.unit_code LIKE ? OR cu.unit_name_raw LIKE ? OR cu.api_definition LIKE ?)")
        values.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])
    if status:
        clauses.append("cu.api_match_status = ?")
        values.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = safe_limit(params)
    conn = connect_db_for_read(db_path, required_tables=DASHBOARD_LOOKUP_TABLES)
    rows = conn.execute(
        f"""
        SELECT
            cu.unit_code, cu.unit_name_raw, cu.unit_level_raw,
            cu.unit_name_refined, cu.api_unit_name, cu.api_unit_level,
            cu.api_definition, cu.api_definition_refined, cu.api_match_status,
            c.major_name, c.middle_name, c.small_name, c.sub_name,
            COUNT(DISTINCT ce.element_id) AS element_count,
            COUNT(DISTINCT CASE WHEN ce.api_match_status = 'matched' THEN ce.element_id END) AS element_matched,
            COUNT(DISTINCT pc.criteria_id) AS criteria_count,
            COUNT(DISTINCT ki.ksa_id) AS ksa_count
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        LEFT JOIN competency_elements ce ON ce.unit_code = cu.unit_code
        LEFT JOIN performance_criteria pc ON pc.element_id = ce.element_id
        LEFT JOIN ksa_items ki ON ki.element_id = ce.element_id
        {where}
        GROUP BY cu.unit_code
        ORDER BY cu.unit_code
        LIMIT ?
        """,
        [*values, limit],
    ).fetchall()
    conn.close()
    return {"units": [dict(row) for row in rows]}


def get_unit_detail(db_path: Path, params: dict[str, list[str]]) -> dict:
    unit_code = first(params, "unit_code")
    if not unit_code:
        raise ValueError("unit_code is required")
    conn = connect_db_for_read(db_path, required_tables=DASHBOARD_LOOKUP_TABLES)
    unit = conn.execute(
        """
        SELECT cu.*, c.major_name, c.middle_name, c.small_name, c.sub_name, c.duty_def_api, c.duty_def_refined
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE cu.unit_code = ?
        """,
        (unit_code,),
    ).fetchone()
    if unit is None:
        conn.close()
        return {"error": "not_found", "unit_code": unit_code}
    elements = conn.execute(
        """
        SELECT
            ce.*,
            COUNT(DISTINCT pc.criteria_id) AS criteria_count,
            COUNT(DISTINCT ki.ksa_id) AS ksa_count
        FROM competency_elements ce
        LEFT JOIN performance_criteria pc ON pc.element_id = ce.element_id
        LEFT JOIN ksa_items ki ON ki.element_id = ce.element_id
        WHERE ce.unit_code = ?
        GROUP BY ce.element_id
        ORDER BY CAST(ce.element_no AS INTEGER), ce.element_id
        """,
        (unit_code,),
    ).fetchall()
    conn.close()
    return {"unit": dict(unit), "elements": [dict(row) for row in elements]}


def get_api_orphans(db_path: Path, params: dict[str, list[str]]) -> dict:
    limit = safe_limit(params)
    conn = connect_db_for_read(db_path, required_tables=API_ORPHAN_LOOKUP_TABLES)
    rows = conn.execute(
        """
        SELECT acu.*
        FROM api_competency_units acu
        LEFT JOIN competency_units cu ON cu.unit_code = acu.ncs_cl_cd
        WHERE cu.unit_code IS NULL
        ORDER BY acu.ncs_cl_cd
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return {"api_orphans": [dict(row) for row in rows]}


def get_issues(db_path: Path, params: dict[str, list[str]]) -> dict:
    limit = safe_limit(params, default=100)
    clauses = ["qi.resolved_at IS NULL"]
    sql_params: list[str | int] = []
    for field in ["target_type", "issue_type", "severity"]:
        value = first(params, field)
        if value:
            clauses.append(f"qi.{field} = ?")
            sql_params.append(value)
    issue_scope, issue_scope_values = quality_issue_scope_filter(params)
    if issue_scope:
        clauses.append(issue_scope)
        sql_params.extend(issue_scope_values)
    where = " AND ".join(clauses)
    try:
        conn = connect_db_for_read(
            db_path,
            required_tables=(
                "quality_issues",
                "classifications",
                "competency_units",
                "competency_elements",
                "performance_criteria",
                "ksa_items",
            ),
        )
    except DashboardReadOnlyError as exc:
        return {"ok": False, "error": exc.to_payload(), "issues": []}
    rows = conn.execute(
        f"""
        SELECT *
        FROM quality_issues qi
        WHERE {where}
        ORDER BY
          CASE qi.severity WHEN 'error' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END,
          qi.issue_id
        LIMIT ?
        """,
        [*sql_params, limit],
    ).fetchall()
    issues = [enrich_issue(conn, dict(row)) for row in rows]
    conn.close()
    return {"issues": issues}


def enrich_issue(conn, issue: dict) -> dict:
    target_type = issue["target_type"]
    target_id = issue["target_id"]
    if target_type == "criteria":
        row = conn.execute(
            """
            SELECT pc.criteria_text_raw AS raw_text, pc.criteria_text_refined AS refined_text,
                   pc.review_status, ce.element_id, ce.element_name_raw AS element_name,
                   ce.unit_code, cu.unit_name_raw AS unit_name
            FROM performance_criteria pc
            JOIN competency_elements ce ON ce.element_id = pc.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE pc.criteria_id = ?
            """,
            (target_id,),
        ).fetchone()
    elif target_type == "ksa":
        row = conn.execute(
            """
            SELECT ki.ksa_text_raw AS raw_text, ki.ksa_text_refined AS refined_text,
                   ki.review_status, ce.element_id, ce.element_name_raw AS element_name,
                   ce.unit_code, cu.unit_name_raw AS unit_name
            FROM ksa_items ki
            JOIN competency_elements ce ON ce.element_id = ki.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE ki.ksa_id = ?
            """,
            (target_id,),
        ).fetchone()
    elif target_type == "element":
        row = conn.execute(
            """
            SELECT ce.element_name_raw AS raw_text, ce.element_name_refined AS refined_text,
                   ce.api_match_status AS review_status, ce.element_id,
                   ce.element_name_raw AS element_name, ce.unit_code,
                   cu.unit_name_raw AS unit_name
            FROM competency_elements ce
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE ce.element_id = ?
            """,
            (target_id,),
        ).fetchone()
    elif target_type == "unit":
        row = conn.execute(
            """
            SELECT cu.unit_name_raw AS raw_text, cu.unit_name_refined AS refined_text,
                   cu.api_match_status AS review_status, cu.unit_code,
                   cu.unit_name_raw AS unit_name
            FROM competency_units cu
            WHERE cu.unit_code = ?
            """,
            (target_id,),
        ).fetchone()
    else:
        row = None
    if row:
        issue.update(dict(row))
    return issue


def save_refined(db_path: Path, payload: dict) -> dict:
    target_type = payload["target_type"]
    target_id = str(payload["target_id"])
    refined_text = str(payload.get("refined_text", "")).strip()
    return save_manual_preprocess(
        db_path,
        {
            "kind": target_type,
            "id": target_id,
            "body_refined": refined_text,
            "title_refined": refined_text,
            "issue_id": payload.get("issue_id"),
        },
    )


def resolve_issue(db_path: Path, payload: dict) -> dict:
    issue_id = payload["issue_id"]
    conn = connect_db(db_path)
    conn.execute("UPDATE quality_issues SET resolved_at = ? WHERE issue_id = ?", (now_utc(), issue_id))
    conn.commit()
    conn.close()
    return {"ok": True, "issue_id": issue_id}


def parse_args() -> argparse.Namespace:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Run local NCS MCP dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db-path", type=Path, default=settings.db_path)
    parser.add_argument(
        "--allow-remote-bind",
        action="store_true",
        help=(
            "Allow binding the dashboard to a non-loopback address. The dashboard "
            "exposes local operator write endpoints, so this is disabled by default."
        ),
    )
    parser.add_argument(
        "--aihr-readiness-json",
        type=Path,
        help="Override NCS_AIHR_READINESS_JSON_PATH for this dashboard process.",
    )
    parser.add_argument(
        "--aihr-agent-queue-status-json",
        type=Path,
        help="Override NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH for this dashboard process.",
    )
    parser.add_argument(
        "--aihr-agent-queue-run-json",
        type=Path,
        help="Override NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH for this dashboard process.",
    )
    return parser.parse_args()


def is_dashboard_loopback_host(host: str | None) -> bool:
    value = str(host or "").strip().lower()
    if value in {"", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def validate_dashboard_bind_host(host: str | None, *, allow_remote_bind: bool = False) -> None:
    if allow_remote_bind or is_dashboard_loopback_host(host):
        return
    raise ValueError(
        "Refusing to bind NCS dashboard to a non-loopback host without "
        "--allow-remote-bind. The dashboard includes operator write endpoints."
    )


DASHBOARD_ROOT_IDENTITY_MARKERS = (
    "NCS-SQF Ontology Workbench",
    "/aihr-live",
)


def probe_dashboard_http_identity(
    host: str,
    port: int,
    *,
    timeout: float = 2.0,
) -> str | None:
    """Return ``ncs_dashboard``, ``foreign``, or ``None`` when unreachable."""
    url = f"http://{host}:{port}/"
    try:
        with urllib_urlopen(url, timeout=timeout) as response:
            body = response.read(8192).decode("utf-8", errors="replace")
    except (OSError, URLError, TimeoutError, ValueError):
        return None
    if all(marker in body for marker in DASHBOARD_ROOT_IDENTITY_MARKERS):
        return "ncs_dashboard"
    return "foreign"


def validate_dashboard_port_identity(host: str, port: int) -> None:
    identity = probe_dashboard_http_identity(host, port)
    if identity != "foreign":
        return
    raise ValueError(
        f"Port {port} on {host} is already serving a different web app. "
        f"Stop the other process or choose another --port. "
        f"Expected NCS dashboard root markers: {list(DASHBOARD_ROOT_IDENTITY_MARKERS)}"
    )


def apply_aihr_artifact_overrides(args: argparse.Namespace) -> None:
    overrides = {
        "NCS_AIHR_READINESS_JSON_PATH": getattr(args, "aihr_readiness_json", None),
        "NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH": getattr(
            args,
            "aihr_agent_queue_status_json",
            None,
        ),
        "NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH": getattr(
            args,
            "aihr_agent_queue_run_json",
            None,
        ),
    }
    for name, path in overrides.items():
        if path is not None:
            os.environ[name] = str(path)


def main() -> None:
    args = parse_args()
    try:
        validate_dashboard_bind_host(
            args.host,
            allow_remote_bind=bool(getattr(args, "allow_remote_bind", False)),
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    try:
        validate_dashboard_port_identity(args.host, args.port)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    apply_aihr_artifact_overrides(args)
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    server.db_path = args.db_path
    print(f"NCS MCP dashboard: http://{args.host}:{args.port}")
    print(f"DB: {args.db_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
