from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from ncs_mcp.db import clamp_limit, normalize_spaces, now_utc, row_to_dict, rows_to_dicts
from ncs_mcp.mapping_policy import REVIEWED_STATUSES


TRUSTED_STATUSES = tuple(sorted(REVIEWED_STATUSES))
DIRECT_SQF_FIELDS = {
    "duty_education_training": "training",
    "duty_qualification": "qualification",
    "duty_career": "career",
    "duty_license": "license",
    "duty_acarr": "academic_career",
    "duty_remark": "remark",
}


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _clean(text: Any) -> str:
    return normalize_spaces("" if text is None else str(text))


def _has_text(text: Any) -> bool:
    value = _clean(text)
    return bool(value) and value not in {"-", "N/A", "n/a", "없음", "해당없음"}


def _summary(text: Any, limit: int = 300) -> str:
    value = _clean(text)
    return value if len(value) <= limit else f"{value[:limit].rstrip()}..."


def _contains(haystack: str, needle: str) -> bool:
    haystack = _clean(haystack).lower()
    needle = _clean(needle).lower()
    return bool(needle) and len(needle) >= 2 and needle in haystack


def _tokens(*texts: Any) -> set[str]:
    found: set[str] = set()
    for text in texts:
        for token in re.findall(r"[0-9A-Za-z가-힣]+", _clean(text)):
            if len(token) >= 2:
                found.add(token.lower())
    return found


def _confidence_grade(score: float) -> str:
    if score >= 0.75:
        return "high"
    if score >= 0.5:
        return "medium"
    if score > 0:
        return "low"
    return "insufficient"


def _direct_conditions(row: sqlite3.Row | dict[str, Any]) -> dict[str, str]:
    return {
        field: _clean(row[field])
        for field in DIRECT_SQF_FIELDS
        if field in row.keys() and _has_text(row[field])  # type: ignore[union-attr]
    }


def _target_from_sqf(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "source_key": row["source_key"],
        "sqf_field": row["sqf_field_name"],
        "sqf_job": row["job_name"],
        "duty_name": row["duty_name"],
        "duty_level": row["duty_level"],
        "duty_level_name": row["duty_level_name"],
        "ncs_lclas_cd": row["ncs_lclas_cd"],
        "ncs_lclas_name": row["ncs_lclas_name"],
    }


def find_sqf_targets(
    conn: sqlite3.Connection,
    *,
    query: str,
    major_code: str | None = None,
    target_source_key: str | None = None,
    target_level: str | None = None,
    limit: int = 3,
) -> list[sqlite3.Row]:
    params: list[Any] = []
    clauses: list[str] = []
    if target_source_key:
        clauses.append("sd.source_key = ?")
        params.append(target_source_key)
    if major_code:
        clauses.append("sd.ncs_lclas_cd = ?")
        params.append(major_code)
    if target_level:
        clauses.append("sd.duty_level = ?")
        params.append(str(target_level))
    if query and not target_source_key:
        like = f"%{query}%"
        clauses.append(
            """
            (
                sd.sqf_field_name LIKE ?
                OR sd.sqf_sub_field_name LIKE ?
                OR sd.job_name LIKE ?
                OR sd.duty_name LIKE ?
                OR sd.duty_definition LIKE ?
                OR sd.duty_level_definition LIKE ?
                OR sd.autonomy_responsibility LIKE ?
            )
            """
        )
        params.extend([like] * 7)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order_like = f"%{query}%"
    return conn.execute(
        f"""
        SELECT sd.*
        FROM sqf_duties sd
        {where}
        ORDER BY
            CASE WHEN sd.duty_name LIKE ? THEN 0 ELSE 1 END,
            CASE WHEN sd.job_name LIKE ? THEN 0 ELSE 1 END,
            CASE WHEN sd.sqf_sub_field_name LIKE ? THEN 0 ELSE 1 END,
            sd.ncs_lclas_cd, sd.sqf_field_name, sd.job_name, sd.duty_level, sd.duty_name
        LIMIT ?
        """,
        params + [order_like, order_like, order_like, clamp_limit(limit, default=3, maximum=20)],
    ).fetchall()


def direct_sqf_evidence(conn: sqlite3.Connection, target: sqlite3.Row) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    job_level = conn.execute(
        "SELECT sqf_job_level_id FROM sqf_job_levels_normalized WHERE sqf_source_key = ?",
        (target["source_key"],),
    ).fetchone()
    if job_level is not None:
        rows = conn.execute(
            """
            SELECT evidence_id, evidence_type, evidence_text, source_field, source, review_status
            FROM sqf_recognition_evidence
            WHERE sqf_job_level_id = ?
            ORDER BY evidence_type, evidence_id
            """,
            (job_level["sqf_job_level_id"],),
        ).fetchall()
        for row in rows:
            evidence.append(
                {
                    "source_table": "sqf_recognition_evidence",
                    "source_id": str(row["evidence_id"]),
                    "source_field": row["source_field"],
                    "evidence_type": row["evidence_type"],
                    "evidence_text": row["evidence_text"],
                    "review_status": row["review_status"],
                }
            )
    seen = {(item["source_field"], item["evidence_text"]) for item in evidence}
    for field, evidence_type in DIRECT_SQF_FIELDS.items():
        if _has_text(target[field]) and (field, _clean(target[field])) not in seen:
            evidence.append(
                {
                    "source_table": "sqf_duties",
                    "source_id": target["source_key"],
                    "source_field": field,
                    "evidence_type": evidence_type,
                    "evidence_text": _clean(target[field]),
                    "review_status": "raw",
                }
            )
    return evidence


def trusted_sqf_ncs_mappings(
    conn: sqlite3.Connection,
    source_key: str,
    *,
    limit: int = 10,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    placeholders = ",".join("?" for _ in TRUSTED_STATUSES)
    rows = conn.execute(
        f"""
        SELECT
            m.match_id, m.source_id, m.target_id, m.relation, m.score,
            m.confidence, m.match_method, m.evidence_text, m.evidence_source,
            m.review_status,
            cu.unit_code, cu.unit_name_raw, cu.unit_level_raw, cu.api_definition,
            c.major_code, c.major_name, c.middle_code, c.middle_name,
            c.small_code, c.small_name, c.sub_code, c.sub_name
        FROM sqf_ncs_matches m
        JOIN competency_units cu ON cu.unit_code = m.target_id
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE m.source_id = ?
          AND m.target_type = 'ncs_competency_unit'
          AND m.review_status IN ({placeholders})
        ORDER BY m.score DESC, m.match_id
        LIMIT ?
        """,
        [source_key, *TRUSTED_STATUSES, clamp_limit(limit, default=10, maximum=100)],
    ).fetchall()
    excluded = {
        row["review_status"]: int(row["count"])
        for row in conn.execute(
            f"""
            SELECT review_status, COUNT(*) AS count
            FROM sqf_ncs_matches
            WHERE source_id = ?
              AND review_status NOT IN ({placeholders})
            GROUP BY review_status
            """,
            [source_key, *TRUSTED_STATUSES],
        ).fetchall()
    }
    mappings: list[dict[str, Any]] = []
    for row in rows:
        mappings.append(
            {
                "match_id": row["match_id"],
                "unit_id": row["unit_code"],
                "unit_code": row["unit_code"],
                "unit_name": row["unit_name_raw"],
                "unit_level": row["unit_level_raw"],
                "review_status": row["review_status"],
                "match_score": row["score"],
                "evidence_summary": _summary(row["evidence_text"]),
                "mapping": {
                    "relation": row["relation"],
                    "confidence": row["confidence"],
                    "match_method": row["match_method"],
                    "evidence_source": row["evidence_source"],
                },
                "classification": {
                    "major_code": row["major_code"],
                    "major_name": row["major_name"],
                    "middle_code": row["middle_code"],
                    "middle_name": row["middle_name"],
                    "small_code": row["small_code"],
                    "small_name": row["small_name"],
                    "sub_code": row["sub_code"],
                    "sub_name": row["sub_name"],
                },
            }
        )
    return mappings, excluded


def sqf_document_evidence(
    conn: sqlite3.Connection,
    source_key: str,
    *,
    min_score: float = 9.0,
    limit: int = 5,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            m.match_id, m.chunk_id, m.relation, m.score, m.method,
            m.evidence_text, m.review_status,
            jl.duty_name, jl.sqf_level, jl.level_name,
            dc.page_start, dc.page_end, dc.text,
            da.asset_name, ds.document_id, ds.title, ds.ontology_role
        FROM sqf_chunk_job_level_matches m
        JOIN sqf_job_levels_normalized jl ON jl.sqf_job_level_id = m.sqf_job_level_id
        JOIN sqf_document_chunks dc ON dc.chunk_id = m.chunk_id
        JOIN sqf_document_assets da ON da.asset_id = dc.asset_id
        JOIN sqf_document_sources ds ON ds.document_id = da.document_id
        WHERE m.sqf_source_key = ?
          AND m.score >= ?
          AND m.review_status != 'rejected'
        ORDER BY m.score DESC, m.match_id
        LIMIT ?
        """,
        (source_key, min_score, clamp_limit(limit, default=5, maximum=50)),
    ).fetchall()
    return [
        {
            "match_id": row["match_id"],
            "document_title": row["title"],
            "asset_filename": row["asset_name"],
            "page_start": row["page_start"],
            "page_end": row["page_end"],
            "chunk_id": row["chunk_id"],
            "chunk_text_summary": _summary(row["evidence_text"] or row["text"], 360),
            "matched_sqf_job_level": f"{row['duty_name']} {row['sqf_level'] or ''}".strip(),
            "evidence_relation": "document_candidate",
            "source_relation": row["relation"],
            "confidence_score": row["score"],
            "document_meta": {
                "document_id": row["document_id"],
                "ontology_role": row["ontology_role"],
                "method": row["method"],
                "review_status": row["review_status"],
            },
        }
        for row in rows
    ]


def ncs_supporting_evidence(
    conn: sqlite3.Connection,
    mappings: list[dict[str, Any]],
    *,
    criteria_per_unit: int = 2,
    ksa_per_unit: int = 5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    ncs_evidence: list[dict[str, Any]] = []
    learning_objectives: list[dict[str, Any]] = []
    concepts: list[dict[str, Any]] = []
    seen_concepts: set[int] = set()
    for mapping in mappings:
        unit_code = mapping["unit_code"]
        learning_objectives.append(
            {
                "unit_code": unit_code,
                "unit_name": mapping["unit_name"],
                "objective": mapping.get("evidence_summary") or mapping["unit_name"],
                "review_status": mapping["review_status"],
            }
        )
        ncs_evidence.append(
            {
                "source_table": "competency_units",
                "source_id": unit_code,
                "evidence_text": mapping.get("evidence_summary") or mapping["unit_name"],
            }
        )
        criteria_rows = conn.execute(
            """
            SELECT pc.criteria_id, pc.criteria_text_raw
            FROM competency_elements ce
            JOIN performance_criteria pc ON pc.element_id = ce.element_id
            WHERE ce.unit_code = ?
            ORDER BY ce.element_id, CAST(pc.criteria_no AS INTEGER), pc.criteria_id
            LIMIT ?
            """,
            (unit_code, criteria_per_unit),
        ).fetchall()
        for row in criteria_rows:
            ncs_evidence.append(
                {
                    "source_table": "performance_criteria",
                    "source_id": str(row["criteria_id"]),
                    "evidence_text": _summary(row["criteria_text_raw"]),
                }
            )
        concept_rows = conn.execute(
            """
            SELECT DISTINCT
                oc.concept_id, oc.concept_name, oc.definition,
                oc.definition_status, oc.review_status, oc.concept_type,
                ki.ksa_id, ki.ksa_text_raw
            FROM competency_elements ce
            JOIN ksa_items ki ON ki.element_id = ce.element_id
            LEFT JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
            LEFT JOIN ontology_concepts oc ON oc.concept_id = kcl.concept_id
            WHERE ce.unit_code = ?
            ORDER BY ki.ksa_type_code, ki.ksa_id
            LIMIT ?
            """,
            (unit_code, ksa_per_unit),
        ).fetchall()
        for row in concept_rows:
            ncs_evidence.append(
                {
                    "source_table": "ksa_items",
                    "source_id": str(row["ksa_id"]),
                    "evidence_text": _summary(row["ksa_text_raw"]),
                }
            )
            concept_id = row["concept_id"]
            if concept_id is None or concept_id in seen_concepts:
                continue
            concepts.append(
                {
                    "concept_id": concept_id,
                    "concept_name": row["concept_name"],
                    "concept_type": row["concept_type"],
                    "definition": row["definition"],
                    "definition_status": row["definition_status"],
                    "review_status": row["review_status"],
                }
            )
            seen_concepts.add(concept_id)
    return ncs_evidence, concepts, learning_objectives


def search_learning_modules(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    major_code: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    clauses: list[str] = []
    if major_code:
        clauses.append("ncs_lclas_cd = ?")
        params.append(major_code)
    if query:
        like = f"%{query}%"
        clauses.append(
            """
            (
                learn_module_name LIKE ?
                OR learn_module_text LIKE ?
                OR ncs_lclas_name LIKE ?
                OR ncs_mclas_name LIKE ?
                OR ncs_sclas_name LIKE ?
                OR ncs_subd_name LIKE ?
            )
            """
        )
        params.extend([like] * 6)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order_params: list[Any] = []
    if query:
        order_params = [f"%{query}%", f"%{query}%"]
        order = "CASE WHEN learn_module_name LIKE ? THEN 0 WHEN learn_module_text LIKE ? THEN 1 ELSE 2 END,"
    else:
        order = ""
    rows = conn.execute(
        f"""
        SELECT *
        FROM ncs_learning_modules
        {where}
        ORDER BY {order} ncs_lclas_cd, ncs_mclas_cd, ncs_sclas_cd, ncs_subd_cd, learn_module_seq
        LIMIT ?
        """,
        params + order_params + [clamp_limit(limit, default=20, maximum=100)],
    ).fetchall()
    modules: list[dict[str, Any]] = []
    for row in rows:
        score = 0.0
        if query and _contains(f"{row['learn_module_name']} {row['learn_module_text']}", query):
            score += 0.4
        if major_code and row["ncs_lclas_cd"] == major_code:
            score += 0.2
        modules.append(
            {
                "learn_module_seq": row["learn_module_seq"],
                "learn_module_name": row["learn_module_name"],
                "learn_module_text": row["learn_module_text"],
                "ncs_classification": {
                    "major_code": row["ncs_lclas_cd"],
                    "major_name": row["ncs_lclas_name"],
                    "middle_code": row["ncs_mclas_cd"],
                    "middle_name": row["ncs_mclas_name"],
                    "small_code": row["ncs_sclas_cd"],
                    "small_name": row["ncs_sclas_name"],
                    "sub_code": row["ncs_subd_cd"],
                    "sub_name": row["ncs_subd_name"],
                },
                "match": {
                    "score": round(score, 3),
                    "method": "classification_keyword",
                },
            }
        )
    return modules


def get_learning_module(conn: sqlite3.Connection, learn_module_seq: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ncs_learning_modules WHERE learn_module_seq = ?",
        (learn_module_seq,),
    ).fetchone()
    if row is None:
        return {"error": "learning_module_not_found", "learn_module_seq": learn_module_seq}
    unit_links = conn.execute(
        """
        SELECT link.*, cu.unit_name_raw AS unit_name
        FROM learning_module_unit_links link
        JOIN competency_units cu ON cu.unit_code = link.unit_code
        WHERE link.learn_module_seq = ?
        ORDER BY link.confidence_score DESC, link.link_id
        """,
        (learn_module_seq,),
    ).fetchall()
    concept_links = conn.execute(
        """
        SELECT link.*, oc.concept_name, oc.concept_type, oc.definition_status, oc.review_status AS concept_review_status
        FROM learning_module_concept_links link
        JOIN ontology_concepts oc ON oc.concept_id = link.concept_id
        WHERE link.learn_module_seq = ?
        ORDER BY link.confidence_score DESC, link.link_id
        """,
        (learn_module_seq,),
    ).fetchall()
    return {
        "module": row_to_dict(row),
        "unit_links": rows_to_dicts(unit_links),
        "concept_links": rows_to_dicts(concept_links),
    }


def _unit_code_set(mappings: list[dict[str, Any]]) -> set[str]:
    return {mapping["unit_code"] for mapping in mappings if mapping.get("unit_code")}


def _score_module(
    module: sqlite3.Row,
    *,
    query: str,
    target: sqlite3.Row,
    mappings: list[dict[str, Any]],
    concepts: list[dict[str, Any]],
    unit_links: list[sqlite3.Row] | None = None,
    concept_links: list[sqlite3.Row] | None = None,
    direct_evidence: list[dict[str, Any]],
    document_evidence: list[dict[str, Any]],
) -> tuple[float, dict[str, Any]]:
    text = f"{module['learn_module_name']} {module['learn_module_text'] or ''}"
    score = 0.0
    reasons: list[str] = []
    mapped_unit_codes = _unit_code_set(mappings)
    linked_unit_codes = {link["unit_code"] for link in unit_links or []}
    if mapped_unit_codes & linked_unit_codes:
        score += 40
        reasons.append("mapped_unit_link")
    mapped_concept_ids = {concept["concept_id"] for concept in concepts if concept.get("concept_id") is not None}
    linked_concept_ids = {link["concept_id"] for link in concept_links or []}
    concept_link_hits = mapped_concept_ids & linked_concept_ids
    if concept_link_hits:
        score += min(25, len(concept_link_hits) * 10)
        reasons.append("mapped_concept_link")
    if module["ncs_lclas_cd"] and module["ncs_lclas_cd"] == target["ncs_lclas_cd"]:
        score += 20
        reasons.append("major_code_match")
    if _contains(text, query):
        score += 20
        reasons.append("query_text_match")
    for mapping in mappings:
        cls = mapping["classification"]
        if (
            module["ncs_lclas_cd"] == cls["major_code"]
            and module["ncs_mclas_cd"] == cls["middle_code"]
            and module["ncs_sclas_cd"] == cls["small_code"]
            and module["ncs_subd_cd"] == cls["sub_code"]
        ):
            score += 25
            reasons.append("mapped_unit_classification_match")
            break
    for mapping in mappings[:5]:
        if _contains(text, mapping["unit_name"]):
            score += 20
            reasons.append("unit_name_text_match")
            break
    concept_hits = 0
    for concept in concepts[:20]:
        if _contains(text, concept["concept_name"]):
            concept_hits += 1
    if concept_hits:
        score += min(25, concept_hits * 5)
        reasons.append("concept_text_match")
    shared_tokens = _tokens(query, target["duty_name"], target["job_name"]) & _tokens(
        module["learn_module_name"], module["learn_module_text"]
    )
    if shared_tokens:
        score += min(15, len(shared_tokens) * 3)
        reasons.append("shared_tokens")
    if direct_evidence:
        score += 5
    if document_evidence:
        score += 5
    return score, {"reasons": sorted(set(reasons)), "raw_score": score}


def _has_strong_module_reason(match: dict[str, Any]) -> bool:
    strong_reasons = {
        "mapped_unit_link",
        "mapped_concept_link",
        "mapped_unit_classification_match",
        "unit_name_text_match",
        "query_text_match",
        "concept_text_match",
    }
    return bool(strong_reasons & set(match.get("reasons") or []))


def _module_candidates(
    conn: sqlite3.Connection,
    *,
    query: str,
    target: sqlite3.Row,
    mappings: list[dict[str, Any]],
    concepts: list[dict[str, Any]],
    direct_evidence: list[dict[str, Any]],
    document_evidence: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM ncs_learning_modules
        WHERE (? IS NULL OR ncs_lclas_cd = ?)
        ORDER BY ncs_lclas_cd, ncs_mclas_cd, ncs_sclas_cd, ncs_subd_cd, learn_module_seq
        """,
        (target["ncs_lclas_cd"], target["ncs_lclas_cd"]),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        unit_links = conn.execute(
            """
            SELECT unit_code
            FROM learning_module_unit_links
            WHERE learn_module_seq = ?
            """,
            (row["learn_module_seq"],),
        ).fetchall()
        concept_links = conn.execute(
            """
            SELECT concept_id
            FROM learning_module_concept_links
            WHERE learn_module_seq = ?
            """,
            (row["learn_module_seq"],),
        ).fetchall()
        score, match = _score_module(
            row,
            query=query,
            target=target,
            mappings=mappings,
            concepts=concepts,
            unit_links=unit_links,
            concept_links=concept_links,
            direct_evidence=direct_evidence,
            document_evidence=document_evidence,
        )
        if score <= 0:
            continue
        if not _has_strong_module_reason(match):
            continue
        candidates.append(
            {
                "module": row_to_dict(row),
                "score": score,
                "match": match,
            }
        )
    candidates.sort(key=lambda item: (-item["score"], item["module"]["learn_module_seq"]))
    return candidates[: clamp_limit(limit, default=5, maximum=20)]


def _fallback_candidates(
    query: str,
    target: sqlite3.Row,
    *,
    learning_objectives: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    max_items = clamp_limit(limit, default=5, maximum=20)
    fallback_rows = learning_objectives[:max_items] or [
        {
            "unit_code": None,
            "unit_name": target["duty_name"] or query,
            "objective": (
                "No cached NCS learning module matched. Use the mapped NCS units and KSA "
                "as learning objectives."
            ),
            "review_status": "fallback",
        }
    ]
    candidates: list[dict[str, Any]] = []
    for objective in fallback_rows:
        unit_name = objective.get("unit_name") or target["duty_name"] or query
        unit_code = objective.get("unit_code")
        candidates.append(
            {
                "module": {
                    "learn_module_seq": None,
                    "learn_module_name": f"NCS-derived education plan: {unit_name}",
                    "learn_module_text": objective.get("objective")
                    or "Use NCS performance criteria and KSA as learning objectives.",
                    "ncs_lclas_cd": target["ncs_lclas_cd"],
                    "ncs_lclas_name": target["ncs_lclas_name"],
                    "ncs_mclas_cd": None,
                    "ncs_mclas_name": None,
                    "ncs_sclas_cd": None,
                    "ncs_sclas_name": None,
                    "ncs_subd_cd": None,
                    "ncs_subd_name": unit_name,
                    "derived_from_unit_code": unit_code,
                    "derived_from_unit_name": unit_name,
                },
                "score": 35.0,
                "match": {
                    "reasons": ["ncs_fallback", "ontology_derived_learning_objective"],
                    "raw_score": 35.0,
                    "derived_from_unit_code": unit_code,
                    "derived_from_unit_name": unit_name,
                },
            }
        )
    return candidates


def _fallback_candidate(query: str, target: sqlite3.Row) -> dict[str, Any]:
    return _fallback_candidates(query, target, learning_objectives=[], limit=1)[0]


def _combined_evidence(
    *,
    direct_evidence: list[dict[str, Any]],
    document_evidence: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    concepts: list[dict[str, Any]],
    module: dict[str, Any],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for item in direct_evidence[:4]:
        evidence.append(
            {
                "evidence_type": "sqf_direct",
                "source_table": item["source_table"],
                "source_id": item["source_id"],
                "evidence_text": item["evidence_text"],
                "evidence_summary": _summary(item["evidence_text"]),
            }
        )
    for item in document_evidence[:4]:
        evidence.append(
            {
                "evidence_type": "sqf_document",
                "source_table": "sqf_chunk_job_level_matches",
                "source_id": str(item["match_id"]),
                "chunk_id": item["chunk_id"],
                "match_id": item["match_id"],
                "evidence_text": item["chunk_text_summary"],
                "evidence_summary": item["chunk_text_summary"],
                "confidence_score": item["confidence_score"],
            }
        )
    for mapping in mappings[:5]:
        evidence.append(
            {
                "evidence_type": "ncs_mapping",
                "source_table": "sqf_ncs_matches",
                "source_id": str(mapping["match_id"]),
                "match_id": mapping["match_id"],
                "unit_code": mapping["unit_code"],
                "evidence_text": mapping["evidence_summary"],
                "evidence_summary": mapping["evidence_summary"],
                "confidence_score": mapping["match_score"],
            }
        )
    for concept in concepts[:5]:
        evidence.append(
            {
                "evidence_type": "ontology_concept",
                "source_table": "ontology_concepts",
                "source_id": str(concept["concept_id"]),
                "concept_id": concept["concept_id"],
                "evidence_text": concept["concept_name"],
                "evidence_summary": concept["definition"] or concept["concept_name"],
            }
        )
    if module.get("learn_module_seq"):
        evidence.append(
            {
                "evidence_type": "learning_module",
                "source_table": "ncs_learning_modules",
                "source_id": module["learn_module_seq"],
                "learn_module_seq": module["learn_module_seq"],
                "evidence_text": module["learn_module_name"],
                "evidence_summary": _summary(module.get("learn_module_text")),
            }
        )
    if not evidence:
        evidence.append(
            {
                "evidence_type": "weak_fallback",
                "source_table": "sqf_duties",
                "source_id": module.get("learn_module_seq"),
                "evidence_text": "No strong evidence was available for this recommendation.",
                "evidence_summary": "No strong evidence was available.",
            }
        )
    return evidence


def _save_recommendation(
    conn: sqlite3.Connection,
    *,
    query: str,
    request_payload: dict[str, Any],
    target: dict[str, Any],
    summary: dict[str, Any],
    recommendations: list[dict[str, Any]],
    audit: dict[str, Any],
) -> int:
    timestamp = now_utc()
    cur = conn.execute(
        """
        INSERT INTO education_recommendation_runs(
            query, target_source_key, request_payload, target_payload,
            summary_payload, audit_payload, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            query,
            target.get("source_key"),
            _json(request_payload),
            _json(target),
            _json(summary),
            _json(audit),
            timestamp,
        ),
    )
    run_id = int(cur.lastrowid)
    for item in recommendations:
        cur = conn.execute(
            """
            INSERT INTO education_recommendation_items(
                run_id, rank, learn_module_seq, learn_module_name,
                recommendation_payload, confidence_score, confidence_grade, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                item["rank"],
                item.get("learn_module_seq"),
                item.get("learn_module_name"),
                _json(item),
                item["confidence_score"],
                item["confidence_grade"],
                timestamp,
            ),
        )
        item_id = int(cur.lastrowid)
        item["recommendation_item_id"] = item_id
        conn.execute(
            "UPDATE education_recommendation_items SET recommendation_payload = ? WHERE item_id = ?",
            (_json(item), item_id),
        )
        for evidence in item.get("evidence", []):
            conn.execute(
                """
                INSERT INTO education_recommendation_evidence(
                    run_id, item_id, evidence_type, source_table, source_id,
                    chunk_id, match_id, unit_code, concept_id, learn_module_seq,
                    evidence_text, evidence_summary, confidence_score, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    item_id,
                    evidence.get("evidence_type"),
                    evidence.get("source_table"),
                    evidence.get("source_id"),
                    evidence.get("chunk_id"),
                    evidence.get("match_id"),
                    evidence.get("unit_code"),
                    evidence.get("concept_id"),
                    evidence.get("learn_module_seq"),
                    evidence.get("evidence_text"),
                    evidence.get("evidence_summary"),
                    evidence.get("confidence_score"),
                    timestamp,
                ),
            )
    return run_id


def recommend_education_for_duty(
    conn: sqlite3.Connection,
    *,
    query: str,
    major_code: str | None = None,
    target_source_key: str | None = None,
    target_level: str | None = None,
    current_concepts: list[str] | None = None,
    limit: int = 5,
    save: bool = True,
) -> dict[str, Any]:
    max_items = clamp_limit(limit, default=5, maximum=20)
    targets = find_sqf_targets(
        conn,
        query=query,
        major_code=major_code,
        target_source_key=target_source_key,
        target_level=target_level,
        limit=1,
    )
    if not targets:
        return {
            "ok": False,
            "error": {"code": "SQF_TARGET_NOT_FOUND", "message": "No SQF duty matched the request."},
            "query": query,
            "recommendations": [],
        }
    target_row = targets[0]
    target = _target_from_sqf(target_row)
    direct = direct_sqf_evidence(conn, target_row)
    mappings, excluded_counts = trusted_sqf_ncs_mappings(conn, target_row["source_key"], limit=10)
    document = sqf_document_evidence(conn, target_row["source_key"], limit=5)
    ncs_evidence, concepts, learning_objectives = ncs_supporting_evidence(conn, mappings)
    candidates = _module_candidates(
        conn,
        query=query,
        target=target_row,
        mappings=mappings,
        concepts=concepts,
        direct_evidence=direct,
        document_evidence=document,
        limit=max_items,
    )
    if not candidates:
        candidates = _fallback_candidates(
            query,
            target_row,
            learning_objectives=learning_objectives,
            limit=max_items,
        )

    recommendations: list[dict[str, Any]] = []
    for rank, candidate in enumerate(candidates, start=1):
        module = candidate["module"]
        normalized_score = min(1.0, float(candidate["score"]) / 100.0)
        limitations: list[str] = []
        if not direct:
            limitations.append("SQF direct education, qualification, career, or license evidence is empty.")
        if not document:
            limitations.append("No SQF document chunk evidence met the score threshold.")
        if not mappings:
            limitations.append("No trusted SQF-NCS mapping was available; candidate mappings were not used.")
        if module.get("learn_module_seq") is None:
            limitations.append("No cached NCS learning module matched; NCS learning objectives are returned as fallback.")
        recommendation_type = (
            "mixed"
            if direct and learning_objectives
            else "sqf_direct"
            if direct
            else "ncs_derived"
        )
        evidence = _combined_evidence(
            direct_evidence=direct,
            document_evidence=document,
            mappings=mappings,
            concepts=concepts,
            module=module,
        )
        item = {
            "rank": rank,
            "learn_module_seq": module.get("learn_module_seq"),
            "learn_module_name": module.get("learn_module_name"),
            "learn_module_text": module.get("learn_module_text"),
            "matched_ncs_units": [
                {
                    "unit_id": mapping["unit_id"],
                    "unit_code": mapping["unit_code"],
                    "unit_name": mapping["unit_name"],
                    "review_status": mapping["review_status"],
                }
                for mapping in mappings
            ],
            "matched_ksa_concepts": concepts,
            "sqf_direct_evidence": direct,
            "sqf_document_evidence": document,
            "ncs_evidence": ncs_evidence,
            "evidence": evidence,
            "confidence_score": round(normalized_score, 3),
            "confidence_grade": _confidence_grade(normalized_score),
            "explanation": (
                f"Recommended for {target['sqf_job']} / {target['duty_name']} because it matches "
                f"{', '.join(candidate['match'].get('reasons') or ['available NCS evidence'])}."
            ),
            "limitations": limitations,
            "match": candidate["match"],
            "recommendation_type": recommendation_type,
            "source_sqf_fields": _direct_conditions(target_row),
            "education": target_row["duty_education_training"],
            "qualification": target_row["duty_qualification"],
            "career": target_row["duty_career"],
            "license": target_row["duty_license"],
            "related_ncs_units": [
                {
                    "unit_code": mapping["unit_code"],
                    "unit_name": mapping["unit_name"],
                    "mapping": {
                        "match_id": mapping["match_id"],
                        "review_status": mapping["review_status"],
                        "score": mapping["match_score"],
                        **mapping["mapping"],
                    },
                }
                for mapping in mappings
            ],
            "learning_objectives": learning_objectives,
            "metadata": {
                "data_source": "SQLite NCS/SQF knowledge base",
                "used_refined_policy": "refined_if_approved",
                "used_mapping_count": len(mappings),
                "excluded_mapping_count": sum(excluded_counts.values()),
                "exclusion_reasons": excluded_counts,
                "candidate_mappings_used": False,
            },
        }
        recommendations.append(item)

    summary = {
        "recommended_modules_count": len([item for item in recommendations if item.get("learn_module_seq")]),
        "direct_sqf_evidence_used": bool(direct),
        "sqf_document_evidence_used": bool(document),
        "ncs_supplement_evidence_used": bool(mappings or ncs_evidence),
        "ontology_concepts_used": len(concepts),
    }
    weak_evidence = sorted({limitation for item in recommendations for limitation in item["limitations"]})
    missing_concepts = [
        {
            "concept_id": concept["concept_id"],
            "concept_name": concept["concept_name"],
            "reason": "Not present in current_concepts input.",
        }
        for concept in concepts
        if current_concepts and concept["concept_name"] not in current_concepts
    ]
    audit = {
        "candidate_mappings_used": False,
        "accepted_mappings_count": len(mappings),
        "sqf_document_chunks_used": len(document),
        "generated_at": now_utc(),
        "data_sources": [
            "sqf_duties",
            "sqf_recognition_evidence",
            "sqf_chunk_job_level_matches",
            "sqf_ncs_matches",
            "ontology_concepts",
            "ncs_learning_modules",
        ],
        "source_ids": [target_row["source_key"]],
        "chunk_ids": [item["chunk_id"] for item in document],
        "match_ids": [mapping["match_id"] for mapping in mappings],
        "learn_module_seqs": [
            item["learn_module_seq"] for item in recommendations if item.get("learn_module_seq")
        ],
        "excluded_mapping_status_counts": excluded_counts,
    }
    request_payload = {
        "query": query,
        "major_code": major_code,
        "target_source_key": target_source_key,
        "target_level": target_level,
        "current_concepts": current_concepts or [],
        "limit": limit,
    }
    run_id: int | None = None
    if save:
        run_id = _save_recommendation(
            conn,
            query=query,
            request_payload=request_payload,
            target=target,
            summary=summary,
            recommendations=recommendations,
            audit=audit,
        )
        conn.commit()
    payload = {
        "ok": True,
        "query": query,
        "recommendation_run_id": run_id,
        "target": target,
        "recommendation_summary": summary,
        "recommendations": recommendations,
        "sqf_document_evidence": document,
        "gaps": {
            "missing_concepts": missing_concepts,
            "weak_evidence_areas": weak_evidence,
        },
        "audit": audit,
        "note": (
            "Recommendations are evidence-based education guidance, not official recognition, "
            "qualification, or legal eligibility decisions."
        ),
    }
    payload["data"] = {
        "recommendation_run_id": run_id,
        "target": target,
        "recommendation_summary": summary,
        "recommendations": recommendations,
        "sqf_document_evidence": document,
        "gaps": payload["gaps"],
    }
    return payload


def get_learning_path_for_sqf_job(
    conn: sqlite3.Connection,
    *,
    query: str,
    major_code: str | None = None,
    target_source_key: str | None = None,
    target_level: str | None = None,
    current_concepts: list[str] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    targets = find_sqf_targets(
        conn,
        query=query,
        major_code=major_code,
        target_source_key=target_source_key,
        target_level=target_level,
        limit=limit,
    )
    path: list[dict[str, Any]] = []
    all_missing: list[dict[str, Any]] = []
    for stage, target_row in enumerate(targets, start=1):
        rec = recommend_education_for_duty(
            conn,
            query=query,
            major_code=major_code,
            target_source_key=target_row["source_key"],
            current_concepts=current_concepts,
            limit=3,
            save=False,
        )
        if not rec.get("ok"):
            continue
        concepts = []
        for item in rec.get("recommendations", []):
            concepts.extend(item.get("matched_ksa_concepts", []))
        missing = rec.get("gaps", {}).get("missing_concepts", [])
        all_missing.extend(missing)
        path.append(
            {
                "stage": stage,
                "duty_level": target_row["duty_level"],
                "target": rec["target"],
                "required_concepts": concepts[:20],
                "recommended_modules": [
                    {
                        "rank": item["rank"],
                        "learn_module_seq": item.get("learn_module_seq"),
                        "learn_module_name": item.get("learn_module_name"),
                        "confidence_score": item.get("confidence_score"),
                        "confidence_grade": item.get("confidence_grade"),
                    }
                    for item in rec.get("recommendations", [])
                ],
                "evidence_summary": rec.get("recommendation_summary", {}),
            }
        )
    payload = {
        "ok": True,
        "path": path,
        "gap_analysis": {
            "missing_concepts": all_missing,
            "stages": len(path),
            "candidate_mappings_used": False,
        },
        "audit": {
            "data_sources": [
                "sqf_duties",
                "sqf_ncs_matches",
                "ontology_concepts",
                "ncs_learning_modules",
            ],
            "target_count": len(targets),
            "generated_at": now_utc(),
        },
    }
    payload["data"] = {"path": path, "gap_analysis": payload["gap_analysis"]}
    return payload


def explain_education_recommendation(
    conn: sqlite3.Connection,
    *,
    recommendation_item_id: int | None = None,
    recommendation_run_id: int | None = None,
    rank: int | None = None,
) -> dict[str, Any]:
    if recommendation_item_id is not None:
        item = conn.execute(
            """
            SELECT *
            FROM education_recommendation_items
            WHERE item_id = ?
            """,
            (recommendation_item_id,),
        ).fetchone()
    elif recommendation_run_id is not None:
        item = conn.execute(
            """
            SELECT *
            FROM education_recommendation_items
            WHERE run_id = ?
              AND (? IS NULL OR rank = ?)
            ORDER BY rank
            LIMIT 1
            """,
            (recommendation_run_id, rank, rank),
        ).fetchone()
    else:
        return {
            "ok": False,
            "error": {
                "code": "RECOMMENDATION_IDENTIFIER_REQUIRED",
                "message": "Provide recommendation_item_id or recommendation_run_id.",
            },
        }
    if item is None:
        return {"ok": False, "error": {"code": "RECOMMENDATION_NOT_FOUND"}}
    evidence_rows = conn.execute(
        """
        SELECT *
        FROM education_recommendation_evidence
        WHERE item_id = ?
        ORDER BY evidence_id
        """,
        (item["item_id"],),
    ).fetchall()
    payload = json.loads(item["recommendation_payload"])
    evidence_chain = rows_to_dicts(evidence_rows)
    payload = {
        "ok": True,
        "recommendation_item": payload,
        "evidence_chain": evidence_chain,
        "confidence_breakdown": {
            "confidence_score": item["confidence_score"],
            "confidence_grade": item["confidence_grade"],
            "evidence_count": len(evidence_chain),
        },
        "limitations": payload.get("limitations", []),
    }
    payload["data"] = {
        "recommendation_item": payload["recommendation_item"],
        "evidence_chain": evidence_chain,
        "confidence_breakdown": payload["confidence_breakdown"],
        "limitations": payload["limitations"],
    }
    return payload
