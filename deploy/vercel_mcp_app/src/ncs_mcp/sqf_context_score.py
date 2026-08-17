from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ncs_mcp.db import normalize_spaces, rows_to_dicts


SCHEMA_VERSION = "ncs_sqf_context_score_report_v1"
UNIT_MATCH_LEVELS = {"unit"}


def _modeling_policy() -> dict[str, Any]:
    return {
        "ncs_subclassification_is_sqf_job": False,
        "ncs_scope_role": "query_resolution_only",
        "sqf_context_granularity": "level_based_job_to_ncs_competency_unit",
        "sqf_level_source": "sqf_job_levels_normalized.sqf_level",
        "ncs_unit_level_used_as_sqf_level": False,
        "ncs_classification_used_in_sqf_score": False,
        "requirement_type_is_mapping_attribute": True,
        "requirement_type_available": False,
        "requirement_type_status": "not_available_in_current_sqf_ncs_matches",
        "required_optional_inferred": False,
        "official_recognition_inferred": False,
        "course_alignment_is_official_recognition": False,
        "unit_identity_rule": "use_full_ncs_unit_code_with_version; do not match by name alone",
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(value: Any) -> str:
    return normalize_spaces("" if value is None else str(value))


def _unit_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "unit_code": row["unit_code"],
        "unit_name_raw": row["unit_name_raw"],
        "unit_level_raw": row["unit_level_raw"],
        "major_code": row["major_code"],
        "major_name": row["major_name"],
        "middle_code": row["middle_code"],
        "middle_name": row["middle_name"],
        "small_code": row["small_code"],
        "small_name": row["small_name"],
        "sub_code": row["sub_code"],
        "sub_name": row["sub_name"],
    }


def _scope_from_units(
    *,
    query: str,
    match_level: str,
    units: list[dict[str, Any]],
    scope_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    first = units[0] if units else (scope_row or {})
    label_parts = [
        first.get("major_name"),
        first.get("middle_name"),
        first.get("small_name"),
        first.get("sub_name"),
    ]
    return {
        "query": query,
        "match_level": match_level,
        "match_text": first.get("unit_name_raw") if match_level == "unit" else first.get("sub_name") or query,
        "classification": {
            "major_code": first.get("major_code"),
            "major_name": first.get("major_name"),
            "middle_code": first.get("middle_code"),
            "middle_name": first.get("middle_name"),
            "small_code": first.get("small_code"),
            "small_name": first.get("small_name"),
            "sub_code": first.get("sub_code"),
            "sub_name": first.get("sub_name"),
        },
        "scope_label": " > ".join(_clean(part) for part in label_parts if _clean(part)),
        "unit_count": len(units),
        "unit_codes": [row["unit_code"] for row in units],
        "units": units[:50],
    }


def resolve_ncs_units_for_sqf_context(
    conn: sqlite3.Connection,
    query: str,
    *,
    major_code: str | None = None,
    unit_limit: int = 200,
) -> dict[str, Any]:
    """Resolve a query to a unit or classification scope for SQF context reporting."""
    query_text = _clean(query)
    if not query_text:
        return _scope_from_units(query=query, match_level="not_found", units=[])

    major_clause = "AND c.major_code = ?" if major_code else ""
    major_params: list[Any] = [major_code] if major_code else []
    unit_rows = rows_to_dicts(
        conn.execute(
            f"""
            SELECT
                cu.unit_code, cu.unit_name_raw, cu.unit_level_raw,
                c.major_code, c.major_name, c.middle_code, c.middle_name,
                c.small_code, c.small_name, c.sub_code, c.sub_name
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE (cu.unit_code = ? OR cu.unit_name_raw = ?)
              {major_clause}
            ORDER BY cu.unit_code
            LIMIT ?
            """,
            (query_text, query_text, *major_params, max(1, int(unit_limit))),
        ).fetchall()
    )
    if unit_rows:
        return _scope_from_units(query=query_text, match_level="unit", units=unit_rows)

    scope_rows = rows_to_dicts(
        conn.execute(
            f"""
            SELECT
                c.classification_id, c.major_code, c.major_name,
                c.middle_code, c.middle_name, c.small_code, c.small_name,
                c.sub_code, c.sub_name
            FROM classifications c
            WHERE (c.sub_name = ? OR c.small_name = ? OR c.middle_name = ? OR c.major_name = ?)
              {major_clause}
            ORDER BY
                CASE
                    WHEN c.sub_name = ? THEN 0
                    WHEN c.small_name = ? THEN 1
                    WHEN c.middle_name = ? THEN 2
                    ELSE 3
                END,
                c.major_code, c.middle_code, c.small_code, c.sub_code
            LIMIT 1
            """,
            (
                query_text,
                query_text,
                query_text,
                query_text,
                *major_params,
                query_text,
                query_text,
                query_text,
            ),
        ).fetchall()
    )
    if scope_rows:
        scope = scope_rows[0]
        units = rows_to_dicts(
            conn.execute(
                """
                SELECT
                    cu.unit_code, cu.unit_name_raw, cu.unit_level_raw,
                    c.major_code, c.major_name, c.middle_code, c.middle_name,
                    c.small_code, c.small_name, c.sub_code, c.sub_name
                FROM competency_units cu
                JOIN classifications c ON c.classification_id = cu.classification_id
                WHERE c.classification_id = ?
                ORDER BY cu.unit_code
                LIMIT ?
                """,
                (scope["classification_id"], max(1, int(unit_limit))),
            ).fetchall()
        )
        level = (
            "sub_classification"
            if scope.get("sub_name") == query_text
            else "small_classification"
            if scope.get("small_name") == query_text
            else "middle_classification"
            if scope.get("middle_name") == query_text
            else "major_classification"
        )
        return _scope_from_units(query=query_text, match_level=level, units=units, scope_row=scope)

    like = f"%{query_text}%"
    fuzzy_units = rows_to_dicts(
        conn.execute(
            f"""
            SELECT
                cu.unit_code, cu.unit_name_raw, cu.unit_level_raw,
                c.major_code, c.major_name, c.middle_code, c.middle_name,
                c.small_code, c.small_name, c.sub_code, c.sub_name
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE (cu.unit_name_raw LIKE ? OR c.sub_name LIKE ? OR c.small_name LIKE ?)
              {major_clause}
            ORDER BY
                CASE WHEN cu.unit_name_raw LIKE ? THEN 0 ELSE 1 END,
                c.major_code, c.middle_code, c.small_code, c.sub_code, cu.unit_code
            LIMIT ?
            """,
            (like, like, like, *major_params, like, max(1, int(unit_limit))),
        ).fetchall()
    )
    if fuzzy_units:
        return _scope_from_units(query=query_text, match_level="fuzzy_unit_or_scope", units=fuzzy_units)
    return _scope_from_units(query=query_text, match_level="not_found", units=[])


def _sqf_candidates_for_units(
    conn: sqlite3.Connection,
    unit_codes: list[str],
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    codes = [code for code in unit_codes if code]
    if not codes:
        return []
    placeholders = ",".join("?" for _ in codes)
    rows = rows_to_dicts(
        conn.execute(
            f"""
            SELECT
                m.match_id, m.source_id, m.target_id AS unit_code, m.relation,
                m.score AS match_score, m.confidence, m.match_method,
                m.review_status, COALESCE(m.filter_status, 'eligible') AS filter_status,
                m.scope_tag, m.evidence_text,
                jl.sqf_job_level_id, jl.duty_name, jl.sqf_level,
                job.sqf_job_id, job.job_name,
                sector.sector_id, sector.sector_name, sector.ncs_lclas_cd,
                sector.ncs_lclas_name, sector.sqf_field_name, sector.sqf_sub_field_name
            FROM sqf_ncs_matches m
            JOIN sqf_job_levels_normalized jl ON jl.sqf_source_key = m.source_id
            JOIN sqf_jobs_normalized job ON job.sqf_job_id = jl.sqf_job_id
            JOIN sqf_industry_sectors sector ON sector.sector_id = job.sector_id
            WHERE m.target_type = 'ncs_competency_unit'
              AND m.target_id IN ({placeholders})
              AND COALESCE(m.filter_status, 'eligible') != 'excluded'
              AND COALESCE(m.review_status, 'candidate') != 'rejected'
            ORDER BY m.score DESC, jl.sqf_level DESC, job.job_name, jl.duty_name, m.target_id
            LIMIT ?
            """,
            (*codes, max(1, int(limit))),
        ).fetchall()
    )
    return rows


def _level_distance(left: Any, right: Any) -> int | None:
    try:
        left_level = int(left)
        right_level = int(right)
    except (TypeError, ValueError):
        return None
    if left_level <= 0 or right_level <= 0:
        return None
    return abs(left_level - right_level)


def _context_pair_score(current: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    same_job = current.get("sqf_job_id") == target.get("sqf_job_id")
    same_sector = current.get("sector_id") == target.get("sector_id")
    same_major = _clean(current.get("ncs_lclas_cd")) and current.get("ncs_lclas_cd") == target.get("ncs_lclas_cd")
    if same_job:
        job_distance = 0
        job_proximity = 1.0
        job_distance_label = "same_sqf_job"
    elif same_sector:
        job_distance = 1
        job_proximity = 0.75
        job_distance_label = "same_sqf_sector"
    else:
        job_distance = 3
        job_proximity = 0.1
        job_distance_label = "different_sqf_context"

    family_match = 1.0 if same_job else 0.75 if same_sector else 0.0
    raw_level_distance = _level_distance(current.get("sqf_level"), target.get("sqf_level"))
    if same_sector and raw_level_distance is not None:
        level_comparison_status = "comparable_same_sqf_sector"
        distance = raw_level_distance
        level_proximity = 0.0 if distance is None else max(0.0, 1.0 - min(distance, 4) / 4)
    elif same_sector:
        level_comparison_status = "level_missing_same_sqf_sector"
        distance = None
        level_proximity = 0.0
    else:
        level_comparison_status = "not_comparable_cross_sector"
        distance = None
        level_proximity = 0.0
    score = round((0.45 * job_proximity) + (0.35 * family_match) + (0.20 * level_proximity), 4)
    label = "high" if score >= 0.75 else "medium" if score >= 0.45 else "low"
    return {
        "sqf_context_score": score,
        "sqf_context_label": label,
        "job_distance": job_distance,
        "job_distance_label": job_distance_label,
        "sqf_level_distance": distance,
        "raw_sqf_level_distance": raw_level_distance,
        "level_comparison_status": level_comparison_status,
        "job_family_match": round(family_match, 4),
        "same_sqf_job": same_job,
        "same_sqf_sector": same_sector,
        "same_ncs_major": bool(same_major),
        "components": {
            "job_proximity": round(job_proximity, 4),
            "job_family_match": round(family_match, 4),
            "level_proximity": round(level_proximity, 4),
        },
        "weights": {
            "job_proximity": 0.45,
            "job_family_match": 0.35,
            "level_proximity": 0.20,
        },
    }


def _scope_guard(current_scope: dict[str, Any], target_scope: dict[str, Any]) -> dict[str, Any]:
    current_level = _clean(current_scope.get("match_level")) or "unknown"
    target_level = _clean(target_scope.get("match_level")) or "unknown"
    active = current_level not in UNIT_MATCH_LEVELS or target_level not in UNIT_MATCH_LEVELS
    return {
        "active": active,
        "status": "classification_scope_only" if active else "unit_scope",
        "current_match_level": current_level,
        "target_match_level": target_level,
        "suppressed_summary_fields": (
            ["top_sqf_context_score", "top_sqf_context_label", "top_job_distance_label", "same_sqf_job"]
            if active
            else []
        ),
        "reason": (
            "At least one query resolved to an NCS classification or fuzzy scope. "
            "This report must not summarize the result as the same SQF job or a high SQF context match."
            if active
            else "Both queries resolved to explicit NCS competency units."
        ),
    }


def _apply_scope_guard(pair: dict[str, Any], guard: dict[str, Any]) -> dict[str, Any]:
    if not guard.get("active"):
        return {**pair, "scope_guard_status": guard.get("status", "unit_scope")}
    guarded = dict(pair)
    guarded.update(
        {
            "raw_sqf_context_score": pair.get("sqf_context_score"),
            "raw_sqf_context_label": pair.get("sqf_context_label"),
            "raw_job_distance_label": pair.get("job_distance_label"),
            "raw_same_sqf_job": pair.get("same_sqf_job"),
            "sqf_context_score": None,
            "sqf_context_label": "classification_scope_only",
            "job_distance_label": "classification_scope_only",
            "same_sqf_job": False,
            "scope_guard_status": "classification_scope_only",
            "scope_guard_reason": guard.get("reason"),
        }
    )
    return guarded


def _mapping_evidence_summary(row: dict[str, Any]) -> str:
    method = _clean(row.get("match_method")) or "unknown"
    status = _clean(row.get("review_status")) or "unknown"
    score = row.get("match_score")
    confidence = _clean(row.get("confidence")) or "unknown"
    return (
        "Candidate SQF-NCS unit mapping from sqf_ncs_matches. "
        f"method={method}; review_status={status}; score={score}; confidence={confidence}. "
        "Raw legacy evidence text is suppressed because it may contain lexical NCS classification "
        "or NCS unit-level wording that is not used as SQF context scoring evidence."
    )


def _candidate_payload(row: dict[str, Any]) -> dict[str, Any]:
    has_legacy_evidence = bool(_clean(row.get("evidence_text")))
    return {
        "unit_code": row.get("unit_code"),
        "source_id": row.get("source_id"),
        "sqf_job_level_id": row.get("sqf_job_level_id"),
        "sector_name": row.get("sector_name"),
        "sqf_field_name": row.get("sqf_field_name"),
        "sqf_sub_field_name": row.get("sqf_sub_field_name"),
        "job_name": row.get("job_name"),
        "duty_name": row.get("duty_name"),
        "sqf_level": row.get("sqf_level"),
        "match_score": row.get("match_score"),
        "match_method": row.get("match_method"),
        "review_status": row.get("review_status"),
        "filter_status": row.get("filter_status"),
        "evidence_summary": _mapping_evidence_summary(row),
        "legacy_evidence_text_status": (
            "suppressed_untrusted_legacy_match_text" if has_legacy_evidence else "not_available"
        ),
        "legacy_evidence_text_suppressed": has_legacy_evidence,
    }


def build_sqf_context_score_report(
    conn: sqlite3.Connection,
    *,
    current_query: str,
    target_query: str,
    current_major_code: str | None = None,
    target_major_code: str | None = None,
    unit_limit: int = 200,
    candidate_limit: int = 200,
    pair_limit: int = 20,
) -> dict[str, Any]:
    """Build a read-only SQF context score report without changing recommendation scoring."""
    current_scope = resolve_ncs_units_for_sqf_context(
        conn,
        current_query,
        major_code=current_major_code,
        unit_limit=unit_limit,
    )
    target_scope = resolve_ncs_units_for_sqf_context(
        conn,
        target_query,
        major_code=target_major_code,
        unit_limit=unit_limit,
    )
    current_candidates = _sqf_candidates_for_units(
        conn,
        current_scope.get("unit_codes") or [],
        limit=candidate_limit,
    )
    target_candidates = _sqf_candidates_for_units(
        conn,
        target_scope.get("unit_codes") or [],
        limit=candidate_limit,
    )
    pairs: list[dict[str, Any]] = []
    for current in current_candidates:
        for target in target_candidates:
            score = _context_pair_score(current, target)
            pairs.append(
                {
                    **score,
                    "current_sqf": _candidate_payload(current),
                    "target_sqf": _candidate_payload(target),
                    "mapping_review_statuses": sorted(
                        {
                            _clean(current.get("review_status")) or "unknown",
                            _clean(target.get("review_status")) or "unknown",
                        }
                    ),
                    "mapping_methods": sorted(
                        {
                            _clean(current.get("match_method")) or "unknown",
                            _clean(target.get("match_method")) or "unknown",
                        }
                    ),
                }
            )
    pairs.sort(
        key=lambda item: (
            -float(item.get("sqf_context_score") or 0.0),
            int(item.get("job_distance") or 99),
            item.get("sqf_level_distance") if item.get("sqf_level_distance") is not None else 99,
            -float((item.get("current_sqf") or {}).get("match_score") or 0.0),
            -float((item.get("target_sqf") or {}).get("match_score") or 0.0),
        )
    )
    unique_pairs: list[dict[str, Any]] = []
    seen_pair_keys: set[tuple[str, str]] = set()
    for pair in pairs:
        current_source = _clean((pair.get("current_sqf") or {}).get("source_id"))
        target_source = _clean((pair.get("target_sqf") or {}).get("source_id"))
        pair_key = (current_source, target_source)
        if pair_key in seen_pair_keys:
            continue
        seen_pair_keys.add(pair_key)
        unique_pairs.append(pair)
    guard = _scope_guard(current_scope, target_scope)
    if guard.get("active"):
        unique_pairs = [_apply_scope_guard(pair, guard) for pair in unique_pairs]
    else:
        unique_pairs = [_apply_scope_guard(pair, guard) for pair in unique_pairs]
    top_pairs = unique_pairs[: max(1, int(pair_limit))]
    top_pair = top_pairs[0] if top_pairs else None
    review_status_counts: dict[str, int] = {}
    for row in [*current_candidates, *target_candidates]:
        key = _clean(row.get("review_status")) or "unknown"
        review_status_counts[key] = review_status_counts.get(key, 0) + 1
    context_available = bool(top_pair)
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": _now(),
        "ok": True,
        "status": "review_required" if context_available else "insufficient_sqf_context",
        "context_available": context_available,
        "context_only": True,
        "recommendation_score_mutated": False,
        "sqf_used_as_training_score": False,
        "approval_ready": False,
        "status_update_allowed": False,
        "modeling_policy": _modeling_policy(),
        "scope_guard": guard,
        "inputs": {
            "current_query": current_query,
            "target_query": target_query,
            "current_major_code": current_major_code,
            "target_major_code": target_major_code,
            "unit_limit": unit_limit,
            "candidate_limit": candidate_limit,
            "pair_limit": pair_limit,
        },
        "resolved_scopes": {
            "current": current_scope,
            "target": target_scope,
        },
        "summary": {
            "current_unit_count": current_scope.get("unit_count", 0),
            "target_unit_count": target_scope.get("unit_count", 0),
            "current_sqf_candidate_count": len(current_candidates),
            "target_sqf_candidate_count": len(target_candidates),
            "pair_count": len(pairs),
            "unique_sqf_pair_count": len(unique_pairs),
            "top_sqf_context_score": top_pair.get("sqf_context_score") if top_pair else 0.0,
            "top_sqf_context_label": top_pair.get("sqf_context_label") if top_pair else "unavailable",
            "top_job_distance_label": top_pair.get("job_distance_label") if top_pair else "unavailable",
            "top_sqf_level_distance": top_pair.get("sqf_level_distance") if top_pair else None,
            "top_level_comparison_status": top_pair.get("level_comparison_status") if top_pair else "unavailable",
            "scope_guard_status": guard.get("status"),
            "classification_scope_only": bool(guard.get("active")),
            "mapping_review_status_counts": review_status_counts,
        },
        "score_interpretation": {
            "purpose": "Explain job-world proximity that KSA overlap alone may miss.",
            "not_used_for": ["training_course_ranking", "official_approval", "trusted_status_update"],
            "formula": "0.45*job_proximity + 0.35*job_family_match + 0.20*level_proximity",
            "job_context_rule": (
                "SQF score uses SQF job and SQF sector. NCS classification matches are kept as diagnostics "
                "and are not used as SQF job proximity."
            ),
            "level_rule": (
                "SQF levels are compared only inside the same SQF sector. "
                "Across sectors, raw level numbers are shown but excluded from level_proximity. "
                "Missing or non-positive SQF levels are reported as level_missing_same_sqf_sector."
            ),
            "labels": {"high": ">= 0.75", "medium": ">= 0.45", "low": "< 0.45"},
        },
        "top_pairs": top_pairs,
        "notes": [
            "SQF context is report-only and candidate evidence until a separate human-reviewed workflow exists.",
            "NCS classifications are used only to resolve query scope; NCS sub-classification is not treated as an SQF job.",
            "Required/optional unit status belongs to the SQF level-based-job to NCS-unit mapping relation; the current sqf_ncs_matches table does not provide it, so this report does not infer it.",
            "SQF-NCS mappings are candidate lexical matches in the current database, so this report explains context but does not approve transferability.",
            "When either query resolves to an NCS classification scope instead of a specific competency unit, same-SQF-job and high-context summaries are suppressed as classification_scope_only.",
            "Recommendation scoring remains NCS KSA and ontology based; SQF context is a third explainability axis only.",
        ],
    }


def write_sqf_context_score_json(report: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _md_level(value: Any) -> str:
    try:
        level = int(value)
    except (TypeError, ValueError):
        return "L-"
    return f"L{level}" if level > 0 else "L-"


def write_sqf_context_score_markdown(report: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary = report.get("summary") or {}
    scopes = report.get("resolved_scopes") or {}
    guard = report.get("scope_guard") or {}
    lines = [
        "# SQF Context Score Report",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- status: `{report.get('status')}`",
        f"- context_only: `{str(report.get('context_only')).lower()}`",
        f"- recommendation_score_mutated: `{str(report.get('recommendation_score_mutated')).lower()}`",
        f"- sqf_used_as_training_score: `{str(report.get('sqf_used_as_training_score')).lower()}`",
        f"- approval_ready: `{str(report.get('approval_ready')).lower()}`",
        f"- status_update_allowed: `{str(report.get('status_update_allowed')).lower()}`",
        "",
        "## Modeling Policy",
        "",
        "| Rule | Value |",
        "| --- | --- |",
    ]
    policy = report.get("modeling_policy") or {}
    for key in [
        "ncs_subclassification_is_sqf_job",
        "ncs_scope_role",
        "sqf_context_granularity",
        "sqf_level_source",
        "ncs_unit_level_used_as_sqf_level",
        "ncs_classification_used_in_sqf_score",
        "requirement_type_is_mapping_attribute",
        "requirement_type_available",
        "required_optional_inferred",
        "official_recognition_inferred",
        "course_alignment_is_official_recognition",
        "unit_identity_rule",
    ]:
        lines.append(f"| `{_md(key)}` | {_md(policy.get(key))} |")
    lines.extend(
        [
            "",
            "## Resolved NCS Scopes",
            "",
            "| Side | Query | Match Level | Scope | Units |",
            "| --- | --- | --- | --- | ---: |",
        ]
    )
    for side in ("current", "target"):
        scope = scopes.get(side) or {}
        lines.append(
            "| {side} | {query} | {level} | {scope} | {units} |".format(
                side=side,
                query=_md(scope.get("query")),
                level=_md(scope.get("match_level")),
                scope=_md(scope.get("scope_label")),
                units=int(scope.get("unit_count") or 0),
            )
        )
    lines.extend(
        [
            "",
            "## Scope Guard",
            "",
            f"- status: `{_md(guard.get('status'))}`",
            f"- active: `{str(bool(guard.get('active'))).lower()}`",
            f"- current_match_level: `{_md(guard.get('current_match_level'))}`",
            f"- target_match_level: `{_md(guard.get('target_match_level'))}`",
            f"- reason: {_md(guard.get('reason'))}",
            f"- suppressed_summary_fields: `{json.dumps(guard.get('suppressed_summary_fields') or [], ensure_ascii=False)}`",
        ]
    )
    lines.extend(
        [
            "",
            "## Summary",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
        ]
    )
    for key in sorted(summary):
        value = summary[key]
        if isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        lines.append(f"| `{_md(key)}` | {_md(value)} |")
    lines.extend(
        [
            "",
            "## Top SQF Pairs",
            "",
            "| Score | Label | Job Distance | Level Distance | Level Rule | Current SQF | Target SQF | Status |",
            "| ---: | --- | --- | ---: | --- | --- | --- | --- |",
        ]
    )
    for pair in report.get("top_pairs") or []:
        current = pair.get("current_sqf") or {}
        target = pair.get("target_sqf") or {}
        score = pair.get("sqf_context_score")
        lines.append(
            "| {score} | {label} | {distance} | {level} | {level_rule} | {current} | {target} | {status} |".format(
                score=_md(score if score is not None else "not_applicable"),
                label=_md(pair.get("sqf_context_label")),
                distance=_md(pair.get("job_distance_label")),
                level=_md(pair.get("sqf_level_distance")),
                level_rule=_md(pair.get("level_comparison_status")),
                current=_md(
                    f"{current.get('sector_name')} / {current.get('job_name')} / {current.get('duty_name')} {_md_level(current.get('sqf_level'))}"
                ),
                target=_md(
                    f"{target.get('sector_name')} / {target.get('job_name')} / {target.get('duty_name')} {_md_level(target.get('sqf_level'))}"
                ),
                status=_md(",".join(pair.get("mapping_review_statuses") or [])),
            )
        )
    if not report.get("top_pairs"):
        lines.append("| 0 | unavailable | unavailable |  | unavailable | none | none |  |")
    lines.extend(["", "## Notes", ""])
    for note in report.get("notes") or []:
        lines.append(f"- {_md(note)}")
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
