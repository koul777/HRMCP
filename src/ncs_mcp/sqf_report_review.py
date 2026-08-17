from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from ncs_mcp.db import clamp_limit, now_utc


FORMAT_VERSION = "ncs-sqf-report-review-seedpack-v1"
DEFAULT_REVIEW_KEYWORDS = ["인사", "노무", "회계", "재무", "세무"]
ALLOWED_DECISIONS = ["approve", "reject", "defer"]
MAX_SNIPPET_CHARS = 700


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


def _normalize_keywords(keywords: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for keyword in keywords or DEFAULT_REVIEW_KEYWORDS:
        text = str(keyword or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized or DEFAULT_REVIEW_KEYWORDS


def _trim_text(value: str | None, *, max_chars: int = MAX_SNIPPET_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "... [truncated]"


def _keyword_where_clause(columns: list[str], keywords: list[str]) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    for keyword in keywords:
        like = f"%{keyword}%"
        keyword_clauses = [f"COALESCE({column}, '') LIKE ?" for column in columns]
        clauses.append("(" + " OR ".join(keyword_clauses) + ")")
        params.extend([like] * len(columns))
    return "(" + " OR ".join(clauses) + ")", params


def _row_to_candidate(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for key in ["sqf_ncs_score", "max_report_score"]:
        if item.get(key) is not None:
            item[key] = float(item[key])
    return item


def _fetch_candidate_mappings(
    conn: sqlite3.Connection,
    *,
    major_code: str | None,
    keywords: list[str],
    limit: int,
    require_report_evidence: bool,
    require_target_keyword: bool,
) -> list[dict[str, Any]]:
    source_columns = [
        "j.job_name",
        "jl.duty_name",
        "jl.duty_definition",
        "jl.job_level_definition",
        "m.evidence_text",
    ]
    target_columns = [
        "cu.unit_name_raw",
        "cu.api_unit_name",
        "c.small_name",
        "c.sub_name",
    ]
    params: list[Any] = []
    where = [
        "m.source_type = 'sqf_duty'",
        "m.target_type = 'ncs_competency_unit'",
        "m.review_status = 'candidate'",
    ]
    source_keyword_clause, source_keyword_params = _keyword_where_clause(source_columns, keywords)
    if require_target_keyword:
        target_keyword_clause, target_keyword_params = _keyword_where_clause(target_columns, keywords)
        where.extend([source_keyword_clause, target_keyword_clause])
        params.extend(source_keyword_params)
        params.extend(target_keyword_params)
    else:
        keyword_clause, keyword_params = _keyword_where_clause(
            [*source_columns, *target_columns],
            keywords,
        )
        where.append(keyword_clause)
        params.extend(keyword_params)
    if major_code:
        where.append("c.major_code = ?")
        params.append(major_code)
    if require_report_evidence:
        where.append(
            """
            EXISTS (
                SELECT 1
                FROM sqf_chunk_job_level_matches cm
                WHERE cm.sqf_source_key = m.source_id
                  AND cm.review_status = 'candidate'
            )
            """
        )

    sql = f"""
        SELECT
            m.match_id AS sqf_ncs_match_id,
            m.source_id AS sqf_source_key,
            m.target_id AS ncs_unit_code,
            m.relation AS sqf_ncs_relation,
            m.score AS sqf_ncs_score,
            m.confidence AS sqf_ncs_confidence,
            m.match_method AS sqf_ncs_match_method,
            m.evidence_text AS sqf_ncs_evidence_text,
            m.evidence_source AS sqf_ncs_evidence_source,
            m.review_status AS sqf_ncs_review_status,
            jl.sqf_job_level_id,
            jl.duty_name,
            jl.sqf_level,
            jl.level_name,
            jl.job_level_definition,
            jl.duty_definition,
            jl.autonomy_responsibility,
            j.sqf_job_id,
            j.job_name,
            j.job_definition,
            s.sector_id,
            s.sector_name,
            s.ncs_lclas_cd AS sqf_major_code,
            s.ncs_lclas_name AS sqf_major_name,
            s.sqf_field_name,
            s.sqf_sub_field_name,
            cu.unit_code,
            cu.unit_name_raw,
            cu.unit_level_raw,
            cu.api_unit_name,
            cu.api_unit_level,
            cu.review_status AS ncs_unit_review_status,
            c.major_code,
            c.major_name,
            c.middle_code,
            c.middle_name,
            c.small_code,
            c.small_name,
            c.sub_code,
            c.sub_name,
            (
                SELECT COUNT(*)
                FROM sqf_chunk_job_level_matches cm
                WHERE cm.sqf_source_key = m.source_id
                  AND cm.review_status = 'candidate'
            ) AS report_evidence_count,
            (
                SELECT MAX(cm.score)
                FROM sqf_chunk_job_level_matches cm
                WHERE cm.sqf_source_key = m.source_id
                  AND cm.review_status = 'candidate'
            ) AS max_report_score
        FROM sqf_ncs_matches m
        JOIN sqf_job_levels_normalized jl ON jl.sqf_source_key = m.source_id
        JOIN sqf_jobs_normalized j ON j.sqf_job_id = jl.sqf_job_id
        JOIN sqf_industry_sectors s ON s.sector_id = j.sector_id
        JOIN competency_units cu ON cu.unit_code = m.target_id
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE {" AND ".join(where)}
        ORDER BY
            report_evidence_count DESC,
            COALESCE(max_report_score, 0) DESC,
            CASE m.relation
                WHEN 'closeMatch' THEN 0
                WHEN 'partiallyCovers' THEN 1
                ELSE 2
            END,
            m.score DESC,
            j.job_name,
            jl.duty_name,
            cu.unit_name_raw
        LIMIT ?
    """
    params.append(limit)
    return [_row_to_candidate(row) for row in conn.execute(sql, params).fetchall()]


def _candidate_keyword_hits(
    candidate: dict[str, Any],
    keywords: list[str],
    *,
    source: bool,
) -> list[str]:
    fields = [
        "job_name",
        "duty_name",
        "duty_definition",
        "job_level_definition",
        "sqf_ncs_evidence_text",
    ]
    if not source:
        fields = [
            "unit_name_raw",
            "api_unit_name",
            "small_name",
            "sub_name",
        ]
    haystack = " ".join(str(candidate.get(field) or "") for field in fields)
    return [keyword for keyword in keywords if keyword in haystack]


def _fetch_report_evidence(
    conn: sqlite3.Connection,
    *,
    sqf_source_key: str,
    keywords: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            cm.match_id AS chunk_match_id,
            cm.relation,
            cm.score,
            cm.method,
            cm.evidence_text,
            cm.matched_terms_json,
            cm.review_status,
            dc.chunk_id,
            dc.chunk_index,
            dc.page_start,
            dc.page_end,
            ds.document_id,
            ds.title AS document_title,
            ds.ontology_role AS document_role,
            da.asset_id,
            da.asset_name
        FROM sqf_chunk_job_level_matches cm
        JOIN sqf_document_chunks dc ON dc.chunk_id = cm.chunk_id
        JOIN sqf_document_assets da ON da.asset_id = dc.asset_id
        JOIN sqf_document_sources ds ON ds.document_id = da.document_id
        WHERE cm.sqf_source_key = ?
          AND cm.review_status = 'candidate'
        ORDER BY cm.score DESC, ds.document_id DESC, dc.page_start, dc.chunk_index
        LIMIT ?
        """,
        (sqf_source_key, max(limit * 4, limit)),
    ).fetchall()
    evidence: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        title = str(item.get("document_title") or "")
        snippet = str(item.get("evidence_text") or "")
        keyword_hits = [
            keyword
            for keyword in keywords
            if keyword and (keyword in title or keyword in snippet)
        ]
        try:
            matched_terms = json.loads(item.get("matched_terms_json") or "{}")
        except json.JSONDecodeError:
            matched_terms = {}
        evidence.append(
            {
                "chunk_match_id": item.get("chunk_match_id"),
                "relation": item.get("relation"),
                "score": float(item.get("score") or 0),
                "method": item.get("method"),
                "review_status": item.get("review_status"),
                "document": {
                    "document_id": item.get("document_id"),
                    "title": item.get("document_title"),
                    "ontology_role": item.get("document_role"),
                    "asset_id": item.get("asset_id"),
                    "asset_name": item.get("asset_name"),
                    "page_start": item.get("page_start"),
                    "page_end": item.get("page_end"),
                    "chunk_id": item.get("chunk_id"),
                    "chunk_index": item.get("chunk_index"),
                },
                "matched_terms": matched_terms,
                "keyword_hits": keyword_hits,
                "evidence_text": _trim_text(snippet),
            }
        )
    evidence.sort(
        key=lambda item: (
            len(item.get("keyword_hits") or []),
            float(item.get("score") or 0),
        ),
        reverse=True,
    )
    return evidence[:limit]


def _candidate_priority(candidate: dict[str, Any], evidence: list[dict[str, Any]]) -> float:
    relation_bonus = 3.0 if candidate.get("sqf_ncs_relation") == "closeMatch" else 1.5
    report_score = max([float(item.get("score") or 0) for item in evidence] or [0.0])
    evidence_bonus = min(len(evidence), 5) * 0.4
    return round(
        float(candidate.get("sqf_ncs_score") or 0)
        + relation_bonus
        + min(report_score, 25.0) * 0.25
        + evidence_bonus,
        3,
    )


def _review_item(
    seedpack_id: str,
    sequence: int,
    candidate: dict[str, Any],
    evidence: list[dict[str, Any]],
    keywords: list[str],
    require_target_keyword: bool,
) -> dict[str, Any]:
    snapshot = {
        "sqf_ncs_match_id": candidate.get("sqf_ncs_match_id"),
        "sqf_source_key": candidate.get("sqf_source_key"),
        "ncs_unit_code": candidate.get("ncs_unit_code"),
        "evidence": evidence,
    }
    return {
        "record_type": "sqf_report_review_item",
        "format_version": FORMAT_VERSION,
        "seedpack_id": seedpack_id,
        "sequence": sequence,
        "priority_score": _candidate_priority(candidate, evidence),
        "target_snapshot_hash": _content_hash(snapshot),
        "decision": "",
        "reviewer_id": "",
        "reviewed_at": "",
        "rationale": "",
        "proposed_sqf_ncs_review_status": "",
        "status_update_allowed": False,
        "used_for_scoring": False,
        "approval_claim": False,
        "recommended_use": "human_review_evidence_only",
        "scope_fit": {
            "source_keyword_hits": _candidate_keyword_hits(candidate, keywords, source=True),
            "target_keyword_hits": _candidate_keyword_hits(candidate, keywords, source=False),
            "target_keyword_required": require_target_keyword,
        },
        "review_questions": [
            "Does the SQF job/duty/level represent the same work scope as the NCS competency unit?",
            "Does the report evidence support this mapping, or is it only a name-level reference?",
            "Can this mapping be used as supplementary education-path context without making a qualification or legal eligibility claim?",
            "Should this remain candidate evidence, be rejected, or be proposed for a later guarded review-status import?",
        ],
        "sqf": {
            "source_key": candidate.get("sqf_source_key"),
            "job_level_id": candidate.get("sqf_job_level_id"),
            "job_id": candidate.get("sqf_job_id"),
            "job_name": candidate.get("job_name"),
            "duty_name": candidate.get("duty_name"),
            "sqf_level": candidate.get("sqf_level"),
            "level_name": candidate.get("level_name"),
            "job_definition": _trim_text(candidate.get("job_definition")),
            "job_level_definition": _trim_text(candidate.get("job_level_definition")),
            "duty_definition": _trim_text(candidate.get("duty_definition")),
            "autonomy_responsibility": _trim_text(candidate.get("autonomy_responsibility")),
            "sector": {
                "sector_id": candidate.get("sector_id"),
                "sector_name": candidate.get("sector_name"),
                "major_code": candidate.get("sqf_major_code"),
                "major_name": candidate.get("sqf_major_name"),
                "field_name": candidate.get("sqf_field_name"),
                "sub_field_name": candidate.get("sqf_sub_field_name"),
            },
        },
        "ncs_candidate": {
            "unit_code": candidate.get("unit_code"),
            "unit_name": candidate.get("unit_name_raw"),
            "unit_level": candidate.get("unit_level_raw"),
            "api_unit_name": candidate.get("api_unit_name"),
            "api_unit_level": candidate.get("api_unit_level"),
            "unit_review_status": candidate.get("ncs_unit_review_status"),
            "classification": {
                "major_code": candidate.get("major_code"),
                "major_name": candidate.get("major_name"),
                "middle_code": candidate.get("middle_code"),
                "middle_name": candidate.get("middle_name"),
                "small_code": candidate.get("small_code"),
                "small_name": candidate.get("small_name"),
                "sub_code": candidate.get("sub_code"),
                "sub_name": candidate.get("sub_name"),
            },
        },
        "sqf_ncs_match": {
            "match_id": candidate.get("sqf_ncs_match_id"),
            "relation": candidate.get("sqf_ncs_relation"),
            "score": candidate.get("sqf_ncs_score"),
            "confidence": candidate.get("sqf_ncs_confidence"),
            "match_method": candidate.get("sqf_ncs_match_method"),
            "evidence_source": candidate.get("sqf_ncs_evidence_source"),
            "review_status": candidate.get("sqf_ncs_review_status"),
            "evidence_text": _trim_text(candidate.get("sqf_ncs_evidence_text")),
        },
        "report_evidence": evidence,
    }


def build_sqf_report_review_seedpack(
    db_path: Path,
    *,
    major_code: str | None = "02",
    keywords: list[str] | None = None,
    limit: int = 40,
    evidence_limit_per_item: int = 3,
    require_report_evidence: bool = True,
    require_target_keyword: bool = True,
) -> dict[str, Any]:
    selected_keywords = _normalize_keywords(keywords)
    max_items = clamp_limit(limit, default=40, maximum=300)
    max_evidence = clamp_limit(evidence_limit_per_item, default=3, maximum=10)
    exported_at = now_utc()
    seedpack_id = "sqf-report-review-" + exported_at.replace(":", "").replace("+00:00", "Z")
    conn = _connect_readonly(db_path)
    try:
        candidates = _fetch_candidate_mappings(
            conn,
            major_code=major_code,
            keywords=selected_keywords,
            limit=max_items,
            require_report_evidence=require_report_evidence,
            require_target_keyword=require_target_keyword,
        )
        items: list[dict[str, Any]] = []
        for sequence, candidate in enumerate(candidates, start=1):
            evidence = _fetch_report_evidence(
                conn,
                sqf_source_key=str(candidate["sqf_source_key"]),
                keywords=selected_keywords,
                limit=max_evidence,
            )
            if require_report_evidence and not evidence:
                continue
            items.append(
                _review_item(
                    seedpack_id,
                    sequence,
                    candidate,
                    evidence,
                    selected_keywords,
                    require_target_keyword,
                )
            )

        relation_counts = dict(Counter(item["sqf_ncs_match"]["relation"] for item in items))
        job_counts = dict(Counter(item["sqf"]["job_name"] for item in items))
        unit_counts = dict(Counter(item["ncs_candidate"]["unit_name"] for item in items))
        doc_counts: Counter[str] = Counter()
        for item in items:
            for evidence in item.get("report_evidence") or []:
                title = ((evidence.get("document") or {}).get("title")) or "unknown"
                doc_counts[str(title)] += 1

        batch_record = {
            "record_type": "batch",
            "format_version": FORMAT_VERSION,
            "seedpack_id": seedpack_id,
            "exported_at": exported_at,
            "major_code": major_code,
            "keywords": selected_keywords,
            "limit": max_items,
            "evidence_limit_per_item": max_evidence,
            "require_report_evidence": require_report_evidence,
            "require_target_keyword": require_target_keyword,
            "item_count": len(items),
            "allowed_decisions": ALLOWED_DECISIONS,
            "status_update_allowed": False,
            "used_for_scoring": False,
            "approval_claim": False,
            "selection_policy": {
                "candidate_review_status_only": True,
                "requires_sqf_ncs_candidate_mapping": True,
                "requires_report_chunk_evidence": require_report_evidence,
                "requires_target_scope_keyword": require_target_keyword,
                "intended_use": "SQF report-grounded Human Review packet, not direct recommendation scoring.",
            },
            "summary": {
                "relation_counts": relation_counts,
                "job_counts": job_counts,
                "unit_counts": unit_counts,
                "document_evidence_counts": dict(doc_counts.most_common(20)),
            },
            "db_fingerprint": _content_hash(
                {
                    "items": [
                        {
                            "sqf_ncs_match_id": item["sqf_ncs_match"]["match_id"],
                            "snapshot": item["target_snapshot_hash"],
                        }
                        for item in items
                    ],
                    "major_code": major_code,
                    "keywords": selected_keywords,
                }
            ),
            "notes": [
                "This seedpack is export-only and must not update review_status by itself.",
                "SQF report evidence is supplementary review material, not official qualification recognition.",
                "Use approved decisions only in a later guarded import flow with explicit human authorization.",
            ],
        }
        return {
            "ok": True,
            "batch": batch_record,
            "items": items,
        }
    finally:
        conn.close()


def write_sqf_report_review_seedpack_jsonl(report: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    records = [report.get("batch") or {}, *(report.get("items") or [])]
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def write_sqf_report_review_seedpack_markdown(report: dict[str, Any], out_path: Path) -> None:
    batch = report.get("batch") or {}
    summary = batch.get("summary") or {}
    items = report.get("items") or []
    lines = [
        "# SQF Report-Grounded Human Review Seedpack",
        "",
        f"- seedpack_id: {batch.get('seedpack_id')}",
        f"- format_version: {batch.get('format_version')}",
        f"- item_count: {batch.get('item_count')}",
        f"- major_code: {batch.get('major_code')}",
        f"- keywords: {', '.join(batch.get('keywords') or [])}",
        f"- require_report_evidence: {str(batch.get('require_report_evidence')).lower()}",
        f"- require_target_keyword: {str(batch.get('require_target_keyword')).lower()}",
        f"- allowed_decisions: {', '.join(batch.get('allowed_decisions') or [])}",
        f"- status_update_allowed: {str(batch.get('status_update_allowed')).lower()}",
        f"- used_for_scoring: {str(batch.get('used_for_scoring')).lower()}",
        f"- approval_claim: {str(batch.get('approval_claim')).lower()}",
        "",
        "## Review Rules",
        "",
        "- Fill `decision` only after a human reviewer inspects the SQF report evidence.",
        "- Do not treat report presence as automatic approval.",
        "- Do not write `human_reviewed`, `accepted`, or `reviewed` from this file alone.",
        "- Use SQF evidence as supplementary job/level context, not as qualification or legal eligibility recognition.",
        "",
        "## Summary",
        "",
        f"- relation_counts: {json.dumps(summary.get('relation_counts') or {}, ensure_ascii=False, sort_keys=True)}",
        f"- job_counts: {json.dumps(summary.get('job_counts') or {}, ensure_ascii=False, sort_keys=True)}",
        f"- unit_counts: {json.dumps(summary.get('unit_counts') or {}, ensure_ascii=False, sort_keys=True)}",
        "",
        "## Top Review Items",
        "",
    ]
    for item in items[:30]:
        sqf = item.get("sqf") or {}
        ncs = item.get("ncs_candidate") or {}
        match = item.get("sqf_ncs_match") or {}
        lines.extend(
            [
                f"### {item.get('sequence')}. {sqf.get('job_name')} / {sqf.get('duty_name')} -> {ncs.get('unit_name')}",
                "",
                f"- priority_score: {item.get('priority_score')}",
                f"- sqf_level: {sqf.get('sqf_level') or ''} {sqf.get('level_name') or ''}".rstrip(),
                f"- ncs_unit_code: {ncs.get('unit_code')}",
                f"- relation: {match.get('relation')} score={match.get('score')} review_status={match.get('review_status')}",
                f"- decision: `{item.get('decision')}`",
                "- report_evidence:",
            ]
        )
        for evidence in (item.get("report_evidence") or [])[:3]:
            document = evidence.get("document") or {}
            page = document.get("page_start")
            if document.get("page_end") and document.get("page_end") != page:
                page = f"{page}-{document.get('page_end')}"
            lines.extend(
                [
                    f"  - {document.get('title')} p.{page} score={evidence.get('score')} relation={evidence.get('relation')}",
                    f"    - {_trim_text(evidence.get('evidence_text'), max_chars=260)}",
                ]
            )
        lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
