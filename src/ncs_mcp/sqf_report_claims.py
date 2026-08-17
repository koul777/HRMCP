from __future__ import annotations

import hashlib
import json
import sqlite3
import csv
from collections import Counter
from html import escape
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ncs_mcp.db import now_utc
from ncs_mcp.sqf_report_review import (
    ALLOWED_DECISIONS,
    FORMAT_VERSION as SEEDPACK_FORMAT_VERSION,
    build_sqf_report_review_seedpack,
)


CLAIM_FORMAT_VERSION = "ncs-sqf-report-claim-candidate-v1"
CORPUS_AUDIT_FORMAT_VERSION = "ncs-sqf-corpus-audit-v1"
DECISION_SHEET_FORMAT_VERSION = "ncs-sqf-report-claim-decision-sheet-v1"
DECISION_SHEET_FIELDS = [
    "order",
    "claim_id",
    "claim_type",
    "recommended_priority",
    "job_name",
    "duty_name",
    "sqf_level",
    "ncs_unit_code",
    "ncs_unit_name",
    "ncs_unit_level",
    "major_code",
    "middle_code",
    "small_code",
    "sub_code",
    "mapping_relation",
    "mapping_score",
    "level_gap",
    "level_status",
    "generic_duty_flag",
    "cross_scope_name_only_risk",
    "evidence_strength",
    "scope_alignment",
    "evidence_ref_count",
    "top_evidence_refs",
    "review_risk_flags",
    "review_action_hint",
    "blocking_rules",
    "review_question",
    "decision",
    "reason",
    "reject_reason_code",
    "defer_reason_code",
    "notes",
    "reviewer_id",
    "reviewed_at",
    "source_packet",
    "status_update_allowed",
    "used_for_scoring",
    "approval_claim",
]
FORBIDDEN_EXPORT_MARKERS = (
    "asset_path",
    "local_path",
    "db_path",
    "source_payload",
    "raw_payload",
    "raw_response",
)


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"SQLite DB does not exist: {path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _content_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _trim_text(value: str | None, *, max_chars: int = 700) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "... [truncated]"


def _parse_level(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _claim_level_gap(sqf: dict[str, Any], ncs: dict[str, Any]) -> int | None:
    sqf_level = _parse_level(sqf.get("sqf_level"))
    ncs_level = _parse_level(ncs.get("api_unit_level") or ncs.get("unit_level"))
    if sqf_level is None or ncs_level is None:
        return None
    if sqf_level == 0 or ncs_level == 0:
        return None
    return abs(sqf_level - ncs_level)


def _level_status(sqf: dict[str, Any], ncs: dict[str, Any], level_gap: int | None) -> str:
    sqf_level = _parse_level(sqf.get("sqf_level"))
    ncs_level = _parse_level(ncs.get("api_unit_level") or ncs.get("unit_level"))
    if sqf_level in (None, 0) or ncs_level in (None, 0):
        return "unknown"
    if level_gap is None:
        return "unknown"
    if level_gap <= 1:
        return "aligned"
    if level_gap == 2:
        return "warning"
    return "mismatch"


def _base_duty_name(value: Any) -> str:
    text = str(value or "").strip()
    if "(" in text:
        text = text.split("(", 1)[0].strip()
    return text


def _generic_duty_flag(sqf: dict[str, Any]) -> bool:
    job_name = str(sqf.get("job_name") or "").strip()
    duty_name = str(sqf.get("duty_name") or "").strip()
    if not job_name or not duty_name:
        return False
    return duty_name == job_name


def _cross_scope_name_only_risk(
    *,
    relation: str | None,
    classification: dict[str, Any],
    sqf: dict[str, Any],
    evidence_strength: str,
) -> bool:
    if str(classification.get("major_code") or "") != "02":
        return True
    if relation == "related":
        return True
    if _generic_duty_flag(sqf) and evidence_strength != "strong":
        return True
    if _base_duty_name(sqf.get("duty_name")) == str(sqf.get("job_name") or "").strip() and relation != "closeMatch":
        return True
    return False


def _evidence_strength(evidence: list[dict[str, Any]]) -> str:
    relations = {str(item.get("relation") or "") for item in evidence}
    if "strongEvidence" in relations:
        return "strong"
    if "supportingEvidence" in relations:
        return "medium"
    if evidence:
        return "weak"
    return "missing"


def _scope_alignment(
    *,
    relation: str | None,
    classification: dict[str, Any],
    level_gap: int | None,
    evidence_strength: str,
    cross_scope_name_only_risk: bool,
) -> str:
    if cross_scope_name_only_risk or str(classification.get("major_code") or "") != "02":
        return "cross_scope_name_only"
    if relation == "closeMatch" and evidence_strength == "strong" and (level_gap is None or level_gap <= 1):
        return "exact_scope"
    if relation in {"closeMatch", "partiallyCovers"} and (level_gap is None or level_gap <= 1):
        return "adjacent_scope"
    if relation in {"closeMatch", "partiallyCovers"}:
        return "broad_scope"
    return "cross_scope_name_only"


def _recommended_priority(
    *,
    relation: str | None,
    classification: dict[str, Any],
    level_gap: int | None,
    evidence_strength: str,
    evidence_count: int,
    level_status: str,
    cross_scope_name_only_risk: bool,
) -> str:
    if str(classification.get("major_code") or "") != "02" or evidence_count < 1 or cross_scope_name_only_risk:
        return "reject_review"
    if relation == "related":
        return "reject_review"
    if level_status == "unknown":
        return "P2"
    if relation == "closeMatch" and evidence_strength == "strong" and (level_gap is None or level_gap <= 1):
        return "P0"
    if relation in {"closeMatch", "partiallyCovers"} and evidence_strength in {"strong", "medium"} and (
        level_gap is None or level_gap <= 1
    ):
        return "P1"
    if relation in {"closeMatch", "partiallyCovers"} and (level_gap is None or level_gap <= 2):
        return "P2"
    return "P3"


def _review_risk_flags(
    *,
    relation: str | None,
    classification: dict[str, Any],
    level_status: str,
    generic_duty_flag: bool,
    cross_scope_name_only_risk: bool,
    evidence_strength: str,
) -> list[str]:
    flags: list[str] = []
    if str(classification.get("major_code") or "") != "02":
        flags.append("target_major_not_02")
    if relation == "related":
        flags.append("related_only_mapping")
    if cross_scope_name_only_risk:
        flags.append("cross_scope_name_only")
    if generic_duty_flag:
        flags.append("generic_duty_scope")
    if level_status in {"warning", "mismatch", "unknown"}:
        flags.append(f"level_{level_status}")
    if evidence_strength in {"medium", "weak", "missing"}:
        flags.append(f"evidence_{evidence_strength}")
    return flags


def _review_action_bundle(
    *,
    claim_id: str,
    claim_type: str,
    ncs: dict[str, Any],
    classification: dict[str, Any],
    relation: str | None,
    evidence_strength: str,
    level_gap: int | None,
    level_status: str,
    scope_alignment: str,
    generic_duty_flag: bool,
    cross_scope_name_only_risk: bool,
) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "claim_type": claim_type,
        "ncs_scope": {
            "unit_code": ncs.get("unit_code"),
            "unit_name": ncs.get("unit_name"),
            "unit_level": ncs.get("api_unit_level") or ncs.get("unit_level"),
            "major_code": classification.get("major_code"),
            "middle_code": classification.get("middle_code"),
            "small_code": classification.get("small_code"),
            "sub_code": classification.get("sub_code"),
        },
        "evidence_strength": evidence_strength,
        "review_risk_flags": _review_risk_flags(
            relation=relation,
            classification=classification,
            level_status=level_status,
            generic_duty_flag=generic_duty_flag,
            cross_scope_name_only_risk=cross_scope_name_only_risk,
            evidence_strength=evidence_strength,
        ),
        "decision_facets": {
            "approve_for_reference": {
                "decision": "approve",
                "effect": "pre_import_annotation_only",
                "requires": ["reviewer_id", "reviewed_at", "reason", "source_packet", "top_evidence_refs"],
            },
            "reject": {
                "decision": "reject",
                "effect": "exclude_from_sqf_review_context",
                "requires": ["reviewer_id", "reviewed_at", "reason"],
            },
            "needs_domain_context": {
                "decision": "defer",
                "effect": "keep_pending_for_subject_matter_review",
                "requires": ["reviewer_id", "reviewed_at", "reason"],
            },
        },
        "human_notes_prompt": (
            "Check work-scope fit, level fit, report grounding, and whether SQF should remain "
            "supplementary review context only."
        ),
        "blocking_rules": {
            "status_update_allowed": False,
            "mutates_scoring": False,
            "saves_review_state": False,
            "requires_guarded_import": True,
            "requires_operator_status_mapping_policy": True,
        },
        "diagnostics": {
            "mapping_relation": relation,
            "level_gap": level_gap,
            "level_status": level_status,
            "scope_alignment": scope_alignment,
        },
    }


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["count"] if row is not None else 0)


def _group_counts(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in conn.execute(sql, params).fetchall():
        key = str(row[0] if row[0] not in (None, "") else "unknown")
        counts[key] = int(row[1] or 0)
    return counts


def _local_path_exists(raw_path: str | None) -> bool:
    if not raw_path:
        return False
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.exists()


def _sanitize_document_ref(document: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in document.items()
        if key not in {"asset_path", "local_path", "source_payload", "raw_payload", "raw_response"}
    }


def _sanitize_evidence_ref(evidence: dict[str, Any]) -> dict[str, Any]:
    sanitized = {
        key: value
        for key, value in evidence.items()
        if key not in {"asset_path", "local_path", "source_payload", "raw_payload", "raw_response"}
    }
    document = sanitized.get("document")
    if isinstance(document, dict):
        sanitized["document"] = _sanitize_document_ref(document)
    return sanitized


def build_sqf_corpus_audit(db_path: Path) -> dict[str, Any]:
    """Build a report-only audit of the SQF document and matching corpus."""
    generated_at = now_utc()
    tables = [
        "sqf_library_posts",
        "sqf_library_files",
        "sqf_document_sources",
        "sqf_document_assets",
        "sqf_document_pages",
        "sqf_document_chunks",
        "sqf_duties",
        "sqf_industry_sectors",
        "sqf_jobs_normalized",
        "sqf_job_levels_normalized",
        "sqf_ncs_matches",
        "sqf_chunk_job_level_matches",
        "review_audit_log",
    ]
    conn = _connect_readonly(db_path)
    try:
        table_counts = {table: _count_rows(conn, table) for table in tables}
        file_rows = conn.execute(
            """
            SELECT
                file_id,
                lib_seq,
                sys_dstin_cd,
                original_filename,
                local_path,
                file_size,
                download_status,
                error_message
            FROM sqf_library_files
            """
        ).fetchall()
        official_files = [dict(row) for row in file_rows if str(row["sys_dstin_cd"] or "").upper() != "LOCAL"]
        downloaded_official = [
            row for row in official_files if str(row.get("download_status") or "") == "downloaded"
        ]
        missing_official = [
            {
                "file_id": row.get("file_id"),
                "lib_seq": row.get("lib_seq"),
                "original_filename": row.get("original_filename"),
                "download_status": row.get("download_status"),
                "download_error_present": bool(str(row.get("error_message") or "").strip()),
            }
            for row in downloaded_official
            if not _local_path_exists(row.get("local_path"))
        ]
        existing_bytes = 0
        for row in downloaded_official:
            raw_path = row.get("local_path")
            if not raw_path or not _local_path_exists(raw_path):
                continue
            path = Path(raw_path)
            if not path.is_absolute():
                path = Path.cwd() / path
            try:
                existing_bytes += path.stat().st_size
            except OSError:
                pass

        document_role_counts = _group_counts(
            conn,
            """
            SELECT COALESCE(ontology_role, 'unknown') AS role, COUNT(*)
            FROM sqf_document_sources
            GROUP BY COALESCE(ontology_role, 'unknown')
            ORDER BY COUNT(*) DESC, role
            """,
        )
        document_status_counts = _group_counts(
            conn,
            """
            SELECT COALESCE(text_extraction_status, 'unknown') AS status, COUNT(*)
            FROM sqf_document_sources
            GROUP BY COALESCE(text_extraction_status, 'unknown')
            ORDER BY COUNT(*) DESC, status
            """,
        )
        asset_type_counts = _group_counts(
            conn,
            """
            SELECT COALESCE(asset_type, 'unknown') AS asset_type, COUNT(*)
            FROM sqf_document_assets
            GROUP BY COALESCE(asset_type, 'unknown')
            ORDER BY COUNT(*) DESC, asset_type
            """,
        )
        asset_status_counts = _group_counts(
            conn,
            """
            SELECT COALESCE(extraction_status, 'unknown') AS status, COUNT(*)
            FROM sqf_document_assets
            GROUP BY COALESCE(extraction_status, 'unknown')
            ORDER BY COUNT(*) DESC, status
            """,
        )
        empty_documents = [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                    ds.document_id,
                    ds.title,
                    ds.ontology_role,
                    ds.text_extraction_status,
                    COUNT(DISTINCT da.asset_id) AS asset_count,
                    COUNT(dc.chunk_id) AS chunk_count
                FROM sqf_document_sources ds
                LEFT JOIN sqf_document_assets da ON da.document_id = ds.document_id
                LEFT JOIN sqf_document_chunks dc ON dc.asset_id = da.asset_id
                GROUP BY ds.document_id
                HAVING chunk_count = 0 OR COALESCE(ds.text_extraction_status, '') <> 'extracted'
                ORDER BY ds.document_id
                LIMIT 100
                """
            ).fetchall()
        ]
        chunk_stats = dict(
            conn.execute(
                """
                SELECT
                    COUNT(*) AS chunk_count,
                    COALESCE(SUM(char_count), 0) AS char_count,
                    COALESCE(SUM(token_estimate), 0) AS token_estimate,
                    COALESCE(MAX(char_count), 0) AS max_chunk_chars
                FROM sqf_document_chunks
                """
            ).fetchone()
        )
        page_stats = dict(
            conn.execute(
                """
                SELECT
                    COUNT(*) AS page_count,
                    COALESCE(SUM(char_count), 0) AS char_count,
                    COALESCE(MAX(char_count), 0) AS max_page_chars
                FROM sqf_document_pages
                """
            ).fetchone()
        )
        sqf_ncs_relation_counts = _group_counts(
            conn,
            """
            SELECT COALESCE(relation, 'unknown') AS relation, COUNT(*)
            FROM sqf_ncs_matches
            GROUP BY COALESCE(relation, 'unknown')
            ORDER BY COUNT(*) DESC, relation
            """,
        )
        sqf_ncs_review_counts = _group_counts(
            conn,
            """
            SELECT COALESCE(review_status, 'unknown') AS status, COUNT(*)
            FROM sqf_ncs_matches
            GROUP BY COALESCE(review_status, 'unknown')
            ORDER BY COUNT(*) DESC, status
            """,
        )
        chunk_match_relation_counts = _group_counts(
            conn,
            """
            SELECT COALESCE(relation, 'unknown') AS relation, COUNT(*)
            FROM sqf_chunk_job_level_matches
            GROUP BY COALESCE(relation, 'unknown')
            ORDER BY COUNT(*) DESC, relation
            """,
        )
        chunk_match_review_counts = _group_counts(
            conn,
            """
            SELECT COALESCE(review_status, 'unknown') AS status, COUNT(*)
            FROM sqf_chunk_job_level_matches
            GROUP BY COALESCE(review_status, 'unknown')
            ORDER BY COUNT(*) DESC, status
            """,
        )
        chunk_match_stats = dict(
            conn.execute(
                """
                SELECT
                    COUNT(*) AS match_count,
                    COUNT(DISTINCT chunk_id) AS matched_chunk_count,
                    COUNT(DISTINCT sqf_job_level_id) AS matched_job_level_count,
                    COUNT(DISTINCT sqf_source_key) AS matched_source_key_count,
                    COALESCE(MAX(score), 0) AS max_score,
                    COALESCE(AVG(score), 0) AS avg_score
                FROM sqf_chunk_job_level_matches
                """
            ).fetchone()
        )
        document_evidence_by_role = [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                    COALESCE(ds.ontology_role, 'unknown') AS ontology_role,
                    COUNT(DISTINCT ds.document_id) AS document_count,
                    COUNT(DISTINCT dc.chunk_id) AS matched_chunk_count,
                    COUNT(cm.match_id) AS match_count
                FROM sqf_chunk_job_level_matches cm
                JOIN sqf_document_chunks dc ON dc.chunk_id = cm.chunk_id
                JOIN sqf_document_assets da ON da.asset_id = dc.asset_id
                JOIN sqf_document_sources ds ON ds.document_id = da.document_id
                GROUP BY COALESCE(ds.ontology_role, 'unknown')
                ORDER BY match_count DESC, ontology_role
                """
            ).fetchall()
        ]

        quality_gates = {
            "official_files_downloaded_and_present": len(missing_official) == 0,
            "document_extraction_has_empty_documents": len(empty_documents) > 0,
            "chunk_corpus_present": int(chunk_stats.get("chunk_count") or 0) > 0,
            "precision_matches_present": int(chunk_match_stats.get("match_count") or 0) > 0,
            "human_review_required": True,
            "approval_ready": False,
            "used_for_scoring": False,
        }
        return {
            "ok": True,
            "record_type": "sqf_corpus_audit",
            "format_version": CORPUS_AUDIT_FORMAT_VERSION,
            "generated_at": generated_at,
            "status": "review_required",
            "approval_ready": False,
            "used_for_scoring": False,
            "status_update_allowed": False,
            "summary": {
                "official_file_count": len(official_files),
                "official_downloaded_count": len(downloaded_official),
                "missing_official_downloaded_files": len(missing_official),
                "document_count": table_counts.get("sqf_document_sources", 0),
                "asset_count": table_counts.get("sqf_document_assets", 0),
                "page_count": int(page_stats.get("page_count") or 0),
                "chunk_count": int(chunk_stats.get("chunk_count") or 0),
                "chunk_match_count": int(chunk_match_stats.get("match_count") or 0),
                "sqf_ncs_candidate_count": table_counts.get("sqf_ncs_matches", 0),
                "empty_document_count": len(empty_documents),
            },
            "table_counts": table_counts,
            "file_audit": {
                "official_file_count": len(official_files),
                "official_downloaded_count": len(downloaded_official),
                "download_status_counts": _group_counts(
                    conn,
                    """
                    SELECT COALESCE(download_status, 'unknown') AS status, COUNT(*)
                    FROM sqf_library_files
                    GROUP BY COALESCE(download_status, 'unknown')
                    ORDER BY COUNT(*) DESC, status
                    """,
                ),
                "existing_official_downloaded_bytes": existing_bytes,
                "missing_official_downloaded_files": missing_official[:50],
            },
            "document_extraction": {
                "document_role_counts": document_role_counts,
                "document_status_counts": document_status_counts,
                "asset_type_counts": asset_type_counts,
                "asset_status_counts": asset_status_counts,
                "empty_or_unextracted_documents": empty_documents,
                "page_stats": page_stats,
                "chunk_stats": chunk_stats,
            },
            "matching": {
                "sqf_ncs_relation_counts": sqf_ncs_relation_counts,
                "sqf_ncs_review_status_counts": sqf_ncs_review_counts,
                "chunk_match_relation_counts": chunk_match_relation_counts,
                "chunk_match_review_status_counts": chunk_match_review_counts,
                "chunk_match_stats": chunk_match_stats,
                "document_evidence_by_role": document_evidence_by_role,
            },
            "quality_gates": quality_gates,
            "notes": [
                "This audit is report-only and does not approve SQF-NCS mappings.",
                "SQF report evidence may support Human Review, but it is not direct recommendation scoring input.",
                "Rows must remain candidate until an explicit human decision is imported through a guarded flow.",
            ],
            "next_actions": [
                "Review empty or unextracted SQF documents and decide whether OCR is needed.",
                "Use claim candidate exports for domain expert review before any status update.",
                "Keep SQF evidence supplemental to NCS task/KSA and training-course evidence.",
            ],
        }
    finally:
        conn.close()


def write_sqf_corpus_audit_markdown(report: dict[str, Any], out_path: Path) -> None:
    summary = report.get("summary") or {}
    file_audit = report.get("file_audit") or {}
    extraction = report.get("document_extraction") or {}
    matching = report.get("matching") or {}
    gates = report.get("quality_gates") or {}
    lines = [
        "# SQF Corpus Audit",
        "",
        f"- format_version: {report.get('format_version')}",
        f"- generated_at: {report.get('generated_at')}",
        f"- status: {report.get('status')}",
        f"- approval_ready: {str(report.get('approval_ready')).lower()}",
        f"- used_for_scoring: {str(report.get('used_for_scoring')).lower()}",
        f"- status_update_allowed: {str(report.get('status_update_allowed')).lower()}",
        "",
        "## Summary",
        "",
        f"- official_file_count: {summary.get('official_file_count')}",
        f"- official_downloaded_count: {summary.get('official_downloaded_count')}",
        f"- missing_official_downloaded_files: {summary.get('missing_official_downloaded_files')}",
        f"- document_count: {summary.get('document_count')}",
        f"- asset_count: {summary.get('asset_count')}",
        f"- page_count: {summary.get('page_count')}",
        f"- chunk_count: {summary.get('chunk_count')}",
        f"- chunk_match_count: {summary.get('chunk_match_count')}",
        f"- sqf_ncs_candidate_count: {summary.get('sqf_ncs_candidate_count')}",
        f"- empty_document_count: {summary.get('empty_document_count')}",
        "",
        "## Quality Gates",
        "",
    ]
    for key, value in gates.items():
        rendered = str(value).lower() if isinstance(value, bool) else value
        lines.append(f"- {key}: {rendered}")
    lines.extend(
        [
            "",
            "## File Audit",
            "",
            f"- download_status_counts: {json.dumps(file_audit.get('download_status_counts') or {}, ensure_ascii=False, sort_keys=True)}",
            f"- existing_official_downloaded_bytes: {file_audit.get('existing_official_downloaded_bytes')}",
            "",
            "## Document Extraction",
            "",
            f"- document_role_counts: {json.dumps(extraction.get('document_role_counts') or {}, ensure_ascii=False, sort_keys=True)}",
            f"- document_status_counts: {json.dumps(extraction.get('document_status_counts') or {}, ensure_ascii=False, sort_keys=True)}",
            f"- asset_type_counts: {json.dumps(extraction.get('asset_type_counts') or {}, ensure_ascii=False, sort_keys=True)}",
            f"- asset_status_counts: {json.dumps(extraction.get('asset_status_counts') or {}, ensure_ascii=False, sort_keys=True)}",
            "",
            "## Matching",
            "",
            f"- sqf_ncs_relation_counts: {json.dumps(matching.get('sqf_ncs_relation_counts') or {}, ensure_ascii=False, sort_keys=True)}",
            f"- sqf_ncs_review_status_counts: {json.dumps(matching.get('sqf_ncs_review_status_counts') or {}, ensure_ascii=False, sort_keys=True)}",
            f"- chunk_match_relation_counts: {json.dumps(matching.get('chunk_match_relation_counts') or {}, ensure_ascii=False, sort_keys=True)}",
            f"- chunk_match_review_status_counts: {json.dumps(matching.get('chunk_match_review_status_counts') or {}, ensure_ascii=False, sort_keys=True)}",
            "",
            "## Empty Or Unextracted Documents",
            "",
        ]
    )
    for item in (extraction.get("empty_or_unextracted_documents") or [])[:30]:
        lines.append(
            f"- document_id={item.get('document_id')} role={item.get('ontology_role')} "
            f"status={item.get('text_extraction_status')} chunks={item.get('chunk_count')} "
            f"title={item.get('title')}"
        )
    lines.extend(["", "## Notes", ""])
    for note in report.get("notes") or []:
        lines.append(f"- {note}")
    lines.extend(["", "## Next Actions", ""])
    for action in report.get("next_actions") or []:
        lines.append(f"- {action}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _claim_id_for_item(item: dict[str, Any]) -> str:
    claim_payload = {
        "sqf_source_key": ((item.get("sqf") or {}).get("source_key")),
        "ncs_unit_code": ((item.get("ncs_candidate") or {}).get("unit_code")),
        "sqf_ncs_match_id": ((item.get("sqf_ncs_match") or {}).get("match_id")),
        "target_snapshot_hash": item.get("target_snapshot_hash"),
    }
    return "sqf-claim-" + _content_hash(claim_payload)[:16]


def _claim_from_review_item(
    item: dict[str, Any],
    *,
    claim_batch_id: str,
    sequence: int,
) -> dict[str, Any]:
    sqf = item.get("sqf") or {}
    ncs = item.get("ncs_candidate") or {}
    match = item.get("sqf_ncs_match") or {}
    evidence: list[dict[str, Any]] = []
    document_role_counts: Counter[str] = Counter()
    claim_id = _claim_id_for_item(item)
    for evidence_index, raw_evidence in enumerate(item.get("report_evidence") or [], start=1):
        evidence_item = _sanitize_evidence_ref(dict(raw_evidence))
        evidence_item["evidence_ref_id"] = f"{claim_id}:evidence:{evidence_index}"
        document_role = ((evidence_item.get("document") or {}).get("ontology_role")) or "unknown"
        document_role_counts[str(document_role)] += 1
        evidence.append(evidence_item)
    max_report_score = max([float(e.get("score") or 0) for e in evidence] or [0.0])
    classification = ncs.get("classification") or {}
    relation = match.get("relation")
    level_gap = _claim_level_gap(sqf, ncs)
    level_status = _level_status(sqf, ncs, level_gap)
    evidence_strength = _evidence_strength(evidence)
    generic_duty_flag = _generic_duty_flag(sqf)
    cross_scope_name_only_risk = _cross_scope_name_only_risk(
        relation=relation,
        classification=classification,
        sqf=sqf,
        evidence_strength=evidence_strength,
    )
    scope_alignment = _scope_alignment(
        relation=relation,
        classification=classification,
        level_gap=level_gap,
        evidence_strength=evidence_strength,
        cross_scope_name_only_risk=cross_scope_name_only_risk,
    )
    recommended_priority = _recommended_priority(
        relation=relation,
        classification=classification,
        level_gap=level_gap,
        evidence_strength=evidence_strength,
        evidence_count=len(evidence),
        level_status=level_status,
        cross_scope_name_only_risk=cross_scope_name_only_risk,
    )
    claim_statement = (
        f"SQF '{sqf.get('job_name')}' duty '{sqf.get('duty_name')}' "
        f"may align with NCS competency unit '{ncs.get('unit_name')}'."
    )
    claim_type = "sqf_ncs_alignment"
    return {
        "record_type": "sqf_report_claim_candidate",
        "format_version": CLAIM_FORMAT_VERSION,
        "claim_batch_id": claim_batch_id,
        "claim_id": claim_id,
        "sequence": sequence,
        "source_seedpack_id": item.get("seedpack_id"),
        "source_seedpack_sequence": item.get("sequence"),
        "claim_type": claim_type,
        "claim_status": "candidate_requires_human_review",
        "claim_statement": claim_statement,
        "decision": "",
        "reviewer_id": "",
        "reviewed_at": "",
        "rationale": "",
        "status_update_allowed": False,
        "used_for_scoring": False,
        "approval_claim": False,
        "recommended_use": "supplementary_review_context_only",
        "allowed_use": "supplementary_review_context_only",
        "import_policy": "guarded_human_import_only",
        "review_contract": {
            "requires_explicit_human_decision": True,
            "may_update_db_directly": False,
            "allowed_decisions": ALLOWED_DECISIONS,
            "prohibited_auto_statuses": ["human_reviewed", "accepted", "reviewed"],
            "guarded_import_required_for_status_change": True,
        },
        "review_dimensions": [
            {
                "dimension": "work_scope",
                "question": "Does the SQF duty describe the same work scope as the NCS competency unit?",
            },
            {
                "dimension": "level_fit",
                "question": "Is the SQF level compatible with the NCS unit level and target education stage?",
            },
            {
                "dimension": "report_grounding",
                "question": "Does the report evidence support this mapping beyond a name-level match?",
            },
            {
                "dimension": "product_use",
                "question": "Can the mapping be used only as supplemental education-path context?",
            },
        ],
        "review_questions": item.get("review_questions") or [],
        "scope_fit": item.get("scope_fit") or {},
        "target_snapshot_hash": item.get("target_snapshot_hash"),
        "priority_score": item.get("priority_score"),
        "recommended_priority": recommended_priority,
        "level_gap": level_gap,
        "level_status": level_status,
        "generic_duty_flag": generic_duty_flag,
        "cross_scope_name_only_risk": cross_scope_name_only_risk,
        "evidence_strength": evidence_strength,
        "scope_alignment": scope_alignment,
        "review_action_bundle": _review_action_bundle(
            claim_id=claim_id,
            claim_type=claim_type,
            ncs=ncs,
            classification=classification,
            relation=relation,
            evidence_strength=evidence_strength,
            level_gap=level_gap,
            level_status=level_status,
            scope_alignment=scope_alignment,
            generic_duty_flag=generic_duty_flag,
            cross_scope_name_only_risk=cross_scope_name_only_risk,
        ),
        "basis_strength": {
            "classification": "report_grounded_candidate",
            "mapping_relation": relation,
            "mapping_score": match.get("score"),
            "mapping_confidence": match.get("confidence"),
            "report_evidence_count": len(evidence),
            "max_report_score": max_report_score,
            "document_role_counts": dict(document_role_counts),
            "level_gap": level_gap,
            "level_status": level_status,
            "generic_duty_flag": generic_duty_flag,
            "cross_scope_name_only_risk": cross_scope_name_only_risk,
            "evidence_strength": evidence_strength,
            "scope_alignment": scope_alignment,
        },
        "sqf": sqf,
        "ncs_candidate": ncs,
        "sqf_ncs_match": match,
        "report_evidence": evidence,
    }


def build_sqf_report_claim_candidates(
    db_path: Path,
    *,
    major_code: str | None = "02",
    keywords: list[str] | None = None,
    limit: int = 80,
    evidence_limit_per_claim: int = 3,
    require_report_evidence: bool = True,
    require_target_keyword: bool = True,
) -> dict[str, Any]:
    """Build claim-level Human Review candidates from the existing seedpack contract."""
    seedpack = build_sqf_report_review_seedpack(
        db_path,
        major_code=major_code,
        keywords=keywords,
        limit=limit,
        evidence_limit_per_item=evidence_limit_per_claim,
        require_report_evidence=require_report_evidence,
        require_target_keyword=require_target_keyword,
    )
    batch = seedpack.get("batch") or {}
    exported_at = batch.get("exported_at") or now_utc()
    claim_batch_id = "sqf-report-claim-" + str(exported_at).replace(":", "").replace("+00:00", "Z")
    claims = [
        _claim_from_review_item(item, claim_batch_id=claim_batch_id, sequence=sequence)
        for sequence, item in enumerate(seedpack.get("items") or [], start=1)
    ]
    relation_counts = dict(Counter((claim.get("basis_strength") or {}).get("mapping_relation") for claim in claims))
    job_counts = dict(Counter(((claim.get("sqf") or {}).get("job_name")) for claim in claims))
    batch_record = {
        "record_type": "batch",
        "format_version": CLAIM_FORMAT_VERSION,
        "claim_batch_id": claim_batch_id,
        "source_seedpack_id": batch.get("seedpack_id"),
        "exported_at": exported_at,
        "major_code": major_code,
        "keywords": batch.get("keywords") or keywords or [],
        "limit": batch.get("limit") or limit,
        "evidence_limit_per_claim": batch.get("evidence_limit_per_item") or evidence_limit_per_claim,
        "claim_count": len(claims),
        "status_update_allowed": False,
        "used_for_scoring": False,
        "approval_claim": False,
        "approval_ready": False,
        "status": "review_required",
        "selection_policy": {
            "candidate_review_status_only": True,
            "requires_sqf_ncs_candidate_mapping": True,
            "requires_report_chunk_evidence": require_report_evidence,
            "requires_target_scope_keyword": require_target_keyword,
            "derived_from_seedpack_format": SEEDPACK_FORMAT_VERSION,
        },
        "summary": {
            "relation_counts": relation_counts,
            "job_counts": job_counts,
            "source_seedpack_item_count": batch.get("item_count"),
        },
        "notes": [
            "Claim candidates are review prompts, not approved ontology facts.",
            "Do not write human_reviewed, accepted, or reviewed from this artifact alone.",
            "Use a later guarded import only after explicit human decisions are recorded.",
        ],
    }
    return {
        "ok": True,
        "batch": batch_record,
        "claims": claims,
    }


def write_sqf_report_claim_candidates_jsonl(report: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    records = [report.get("batch") or {}, *(report.get("claims") or [])]
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def write_sqf_report_claim_candidates_markdown(report: dict[str, Any], out_path: Path) -> None:
    batch = report.get("batch") or {}
    claims = report.get("claims") or []
    summary = batch.get("summary") or {}
    lines = [
        "# SQF Report Claim Candidates",
        "",
        f"- claim_batch_id: {batch.get('claim_batch_id')}",
        f"- format_version: {batch.get('format_version')}",
        f"- source_seedpack_id: {batch.get('source_seedpack_id')}",
        f"- claim_count: {batch.get('claim_count')}",
        f"- major_code: {batch.get('major_code')}",
        f"- keywords: {', '.join(batch.get('keywords') or [])}",
        f"- status: {batch.get('status')}",
        f"- approval_ready: {str(batch.get('approval_ready')).lower()}",
        f"- status_update_allowed: {str(batch.get('status_update_allowed')).lower()}",
        f"- used_for_scoring: {str(batch.get('used_for_scoring')).lower()}",
        f"- approval_claim: {str(batch.get('approval_claim')).lower()}",
        "",
        "## Review Rules",
        "",
        "- These records are claim candidates for Human Review, not accepted mappings.",
        "- Reviewers may record `approve`, `reject`, or `defer`, but this file must not directly update the DB.",
        "- SQF report evidence is supplemental context for education-path review, not qualification recognition.",
        "",
        "## Summary",
        "",
        f"- relation_counts: {json.dumps(summary.get('relation_counts') or {}, ensure_ascii=False, sort_keys=True)}",
        f"- job_counts: {json.dumps(summary.get('job_counts') or {}, ensure_ascii=False, sort_keys=True)}",
        "",
        "## Claims",
        "",
    ]
    for claim in claims[:40]:
        sqf = claim.get("sqf") or {}
        ncs = claim.get("ncs_candidate") or {}
        basis = claim.get("basis_strength") or {}
        match = claim.get("sqf_ncs_match") or {}
        action_bundle = claim.get("review_action_bundle") or {}
        lines.extend(
            [
                f"### {claim.get('sequence')}. {sqf.get('job_name')} / {sqf.get('duty_name')} -> {ncs.get('unit_name')}",
                "",
                f"- claim_id: {claim.get('claim_id')}",
                f"- claim_status: {claim.get('claim_status')}",
                f"- relation: {match.get('relation')} score={match.get('score')}",
                f"- review_risk_flags: {', '.join(action_bundle.get('review_risk_flags') or []) or 'none'}",
                f"- human_notes_prompt: {action_bundle.get('human_notes_prompt')}",
                f"- report_evidence_count: {basis.get('report_evidence_count')} max_report_score={basis.get('max_report_score')}",
                f"- decision: `{claim.get('decision')}`",
                "- evidence_refs:",
            ]
        )
        for evidence in (claim.get("report_evidence") or [])[:3]:
            document = evidence.get("document") or {}
            page = document.get("page_start")
            if document.get("page_end") and document.get("page_end") != page:
                page = f"{page}-{document.get('page_end')}"
            lines.extend(
                [
                    f"  - {evidence.get('evidence_ref_id')} {document.get('title')} p.{page} score={evidence.get('score')}",
                    f"    - {_trim_text(evidence.get('evidence_text'), max_chars=260)}",
                ]
            )
        lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _load_claim_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"Claim report must be a JSON object: {path}")
    claims = payload.get("claims")
    if not isinstance(claims, list):
        raise ValueError(f"Claim report missing claims list: {path}")
    return payload


def _decision_sheet_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _decision_sheet_csv_cell(value: Any) -> str:
    text = _decision_sheet_text(value).replace("\r\n", "\n").replace("\r", "\n")
    if text[:1] in {"=", "+", "-", "@"} or text.lstrip()[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def _portable_source_packet(
    source_packet: str | None,
    claim_report_path: Path,
) -> tuple[str, list[dict[str, Any]]]:
    raw = str(source_packet or claim_report_path.name).strip() or claim_report_path.name
    findings: list[dict[str, Any]] = []
    lower = raw.casefold()
    has_forbidden_marker = any(marker.casefold() in lower for marker in FORBIDDEN_EXPORT_MARKERS)
    is_absolute = PureWindowsPath(raw).is_absolute() or PurePosixPath(raw).is_absolute()
    if is_absolute or has_forbidden_marker:
        packet_name = PureWindowsPath(raw).name or PurePosixPath(raw).name or claim_report_path.name
        findings.append(
            {
                "severity": "blocker",
                "code": "source_packet_not_portable",
                "message": "source_packet must be a portable artifact id or relative report reference, not an internal path or raw payload marker.",
            }
        )
        return packet_name, findings
    return raw, findings


def build_sqf_report_claim_decision_sheet(
    claim_report_path: Path,
    *,
    source_packet: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    payload = _load_claim_report(claim_report_path)
    batch = payload.get("batch") or {}
    claims = [claim for claim in payload.get("claims") or [] if isinstance(claim, dict)]
    if limit is not None:
        claims = claims[: max(0, int(limit))]
    rows: list[dict[str, str]] = []
    findings: list[dict[str, Any]] = []
    packet, packet_findings = _portable_source_packet(source_packet, claim_report_path)
    findings.extend(packet_findings)
    for index, claim in enumerate(claims, start=1):
        if claim.get("status_update_allowed") is not False:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "claim_status_update_allowed_not_false",
                    "claim_id": claim.get("claim_id"),
                    "message": "Claim candidates must not allow DB status updates.",
                }
            )
        if claim.get("used_for_scoring") is not False:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "claim_used_for_scoring_not_false",
                    "claim_id": claim.get("claim_id"),
                    "message": "SQF claim candidates must not be active recommendation scoring input.",
                }
            )
        if claim.get("approval_claim") is not False:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "claim_approval_claim_not_false",
                    "claim_id": claim.get("claim_id"),
                    "message": "SQF claim candidates must not assert approval.",
                }
            )
        sqf = claim.get("sqf") or {}
        ncs = claim.get("ncs_candidate") or {}
        classification = ncs.get("classification") or {}
        match = claim.get("sqf_ncs_match") or {}
        action_bundle = claim.get("review_action_bundle") or {}
        evidence_refs = [
            str(evidence.get("evidence_ref_id") or evidence.get("chunk_match_id") or "")
            for evidence in claim.get("report_evidence") or []
            if isinstance(evidence, dict)
        ]
        rows.append(
            {
                "order": str(index),
                "claim_id": _decision_sheet_text(claim.get("claim_id")),
                "claim_type": _decision_sheet_text(claim.get("claim_type")),
                "recommended_priority": _decision_sheet_text(claim.get("recommended_priority")),
                "job_name": _decision_sheet_text(sqf.get("job_name")),
                "duty_name": _decision_sheet_text(sqf.get("duty_name")),
                "sqf_level": _decision_sheet_text(sqf.get("sqf_level")),
                "ncs_unit_code": _decision_sheet_text(ncs.get("unit_code")),
                "ncs_unit_name": _decision_sheet_text(ncs.get("unit_name")),
                "ncs_unit_level": _decision_sheet_text(ncs.get("api_unit_level") or ncs.get("unit_level")),
                "major_code": _decision_sheet_text(classification.get("major_code")),
                "middle_code": _decision_sheet_text(classification.get("middle_code")),
                "small_code": _decision_sheet_text(classification.get("small_code")),
                "sub_code": _decision_sheet_text(classification.get("sub_code")),
                "mapping_relation": _decision_sheet_text(match.get("relation")),
                "mapping_score": _decision_sheet_text(match.get("score")),
                "level_gap": _decision_sheet_text(claim.get("level_gap")),
                "level_status": _decision_sheet_text(claim.get("level_status")),
                "generic_duty_flag": _decision_sheet_text(claim.get("generic_duty_flag")),
                "cross_scope_name_only_risk": _decision_sheet_text(claim.get("cross_scope_name_only_risk")),
                "evidence_strength": _decision_sheet_text(claim.get("evidence_strength")),
                "scope_alignment": _decision_sheet_text(claim.get("scope_alignment")),
                "evidence_ref_count": str(len(evidence_refs)),
                "top_evidence_refs": ";".join(evidence_refs[:5]),
                "review_risk_flags": ";".join(str(flag) for flag in action_bundle.get("review_risk_flags") or []),
                "review_action_hint": _decision_sheet_text(action_bundle.get("human_notes_prompt")),
                "blocking_rules": _decision_sheet_text(action_bundle.get("blocking_rules") or {}),
                "review_question": "approve/reject/defer: SQF report evidence supports this NCS mapping only as supplementary review context?",
                "decision": "",
                "reason": "",
                "reject_reason_code": "",
                "defer_reason_code": "",
                "notes": "",
                "reviewer_id": "",
                "reviewed_at": "",
                "source_packet": packet,
                "status_update_allowed": "false",
                "used_for_scoring": "false",
                "approval_claim": "false",
            }
        )
    blocker_count = sum(1 for finding in findings if finding.get("severity") == "blocker")
    return {
        "ok": blocker_count == 0,
        "record_type": "sqf_report_claim_decision_sheet",
        "format_version": DECISION_SHEET_FORMAT_VERSION,
        "created_at": now_utc(),
        "claim_report_name": claim_report_path.name,
        "source_claim_batch_id": batch.get("claim_batch_id"),
        "source_packet": packet,
        "row_count": len(rows),
        "allowed_decisions": ALLOWED_DECISIONS,
        "approval_claim": False,
        "db_writes": False,
        "status_update_allowed": False,
        "used_for_scoring": False,
        "review_policy": {
            "human_decision_required_for_status_update": True,
            "no_status_updates_performed": True,
            "guarded_import_required_for_status_change": True,
            "allowed_decisions": ALLOWED_DECISIONS,
            "decision_note": "approve/reject/defer are reviewer notes until a separate guarded import validates provenance.",
        },
        "rows": rows,
        "findings": findings,
        "blocker_count": blocker_count,
    }


def write_sqf_report_claim_decision_sheet_csv(report: dict[str, Any], out_path: Path) -> None:
    rows = [row for row in report.get("rows") or [] if isinstance(row, dict)]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DECISION_SHEET_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: _decision_sheet_csv_cell(row.get(field, ""))
                    for field in DECISION_SHEET_FIELDS
                }
            )


def write_sqf_report_claim_decision_sheet_html(report: dict[str, Any], out_path: Path) -> None:
    rows = [row for row in report.get("rows") or [] if isinstance(row, dict)]
    lines = [
        "<!doctype html>",
        '<html lang="ko">',
        "<head>",
        '<meta charset="utf-8">',
        "<title>SQF Claim Human Review Decision Sheet</title>",
        "<style>",
        "body{font-family:Arial,'Malgun Gothic',sans-serif;margin:24px;color:#202124;}",
        "table{border-collapse:collapse;width:100%;font-size:13px;}",
        "th,td{border:1px solid #d0d7de;padding:6px 8px;vertical-align:top;}",
        "th{background:#f6f8fa;text-align:left;position:sticky;top:0;}",
        ".guardrail{background:#fff8c5;border:1px solid #d0a000;padding:10px 12px;margin:12px 0;}",
        ".muted{color:#57606a;}",
        "</style>",
        "</head>",
        "<body>",
        "<h1>SQF Claim Human Review Decision Sheet</h1>",
        '<div class="guardrail">',
        "This sheet collects approve/reject/defer notes only. It does not update DB rows, "
        "does not mark human_reviewed/accepted/reviewed, and is not recommendation scoring input.",
        "</div>",
        "<ul>",
        f"<li>ok: {escape(str(report.get('ok')))}</li>",
        f"<li>source_claim_batch_id: {escape(str(report.get('source_claim_batch_id')))}</li>",
        f"<li>row_count: {escape(str(report.get('row_count')))}</li>",
        f"<li>allowed_decisions: {escape(str(report.get('allowed_decisions')))}</li>",
        f"<li>approval_claim: {escape(str(report.get('approval_claim')))}</li>",
        f"<li>db_writes: {escape(str(report.get('db_writes')))}</li>",
        f"<li>status_update_allowed: {escape(str(report.get('status_update_allowed')))}</li>",
        "</ul>",
        "<table>",
        "<thead><tr>",
    ]
    for field in DECISION_SHEET_FIELDS:
        lines.append(f"<th>{escape(field)}</th>")
    lines.extend(["</tr></thead>", "<tbody>"])
    for row in rows:
        lines.append("<tr>")
        for field in DECISION_SHEET_FIELDS:
            value = row.get(field, "")
            css_class = ' class="muted"' if field in {"decision", "reason", "notes"} and not value else ""
            lines.append(f"<td{css_class}>{escape(str(value))}</td>")
        lines.append("</tr>")
    lines.extend(["</tbody>", "</table>"])
    findings = [finding for finding in report.get("findings") or [] if isinstance(finding, dict)]
    if findings:
        lines.extend(["<h2>Findings</h2>", "<ul>"])
        for finding in findings:
            lines.append(
                "<li>"
                f"{escape(str(finding.get('severity')))}:"
                f"{escape(str(finding.get('code')))} "
                f"{escape(str(finding.get('message')))}"
                "</li>"
            )
        lines.append("</ul>")
    lines.extend(["</body>", "</html>"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
