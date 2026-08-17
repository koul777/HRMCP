from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from ncs_mcp.db import connect, create_indexes, initialize_database, now_utc, rows_to_dicts


TOKEN_RE = re.compile(r"[\uac00-\ud7a3A-Za-z0-9]{2,}")
LEVEL_PATTERNS = [
    r"{level}\s*\uc218\uc900",
    r"\uc218\uc900\s*{level}",
    r"\({level}\)",
    r"L\s*{level}",
]
STOPWORDS = {
    "\uc9c1\ubb34",
    "\uc218\uc900",
    "\uc5ed\ub7c9",
    "\ub2a5\ub825",
    "\uc218\ud589",
    "\uc5c5\ubb34",
    "\uc0b0\uc5c5",
    "\ubd84\uc57c",
    "\uad00\ub828",
    "\uae30\ubc18",
    "\uc704\ud55c",
    "\ub300\ud55c",
    "\uc774\ub97c",
    "\ud1b5\ud574",
    "\ubc0f",
    "SQF",
    "NCS",
}


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def compact(value: str | None) -> str:
    return re.sub(r"\s+", "", normalize_text(value)).lower()


def base_duty_name(value: str | None) -> str:
    text = normalize_text(value)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\bL?\d+\b", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def tokenize(value: str | None) -> list[str]:
    tokens = []
    for token in TOKEN_RE.findall(normalize_text(value)):
        if token in STOPWORDS:
            continue
        if len(token) < 2:
            continue
        tokens.append(token)
    return tokens


def row_terms(row: sqlite3.Row | dict[str, Any]) -> dict[str, list[str]]:
    getter = row.get if isinstance(row, dict) else row.__getitem__
    exact_candidates = [
        getter("duty_name"),
        base_duty_name(getter("duty_name")),
        getter("job_name"),
        getter("sqf_field_name"),
        getter("sqf_sub_field_name"),
        getter("sector_name"),
        getter("ncs_lclas_name"),
    ]
    exact_terms: list[str] = []
    for term in exact_candidates:
        text = normalize_text(term)
        if text and text not in exact_terms:
            exact_terms.append(text)

    support_text = " ".join(
        normalize_text(getter(key))
        for key in [
            "job_level_definition",
            "duty_definition",
            "autonomy_responsibility",
        ]
    )
    support_tokens = []
    for token in tokenize(support_text):
        if token not in support_tokens:
            support_tokens.append(token)
        if len(support_tokens) >= 16:
            break
    return {"exact": exact_terms, "support": support_tokens}


def level_matched(text: str, level: int | None) -> bool:
    if level is None:
        return False
    return any(re.search(pattern.format(level=level), text, flags=re.IGNORECASE) for pattern in LEVEL_PATTERNS)


def snippet_around(text: str, terms: list[str], width: int = 450) -> str:
    for term in terms:
        if not term:
            continue
        position = text.find(term)
        if position >= 0:
            start = max(0, position - width // 2)
            end = min(len(text), position + len(term) + width // 2)
            return text[start:end].strip()
    return text[:width].strip()


def score_chunk_for_job_level(chunk_text: str, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    text = normalize_text(chunk_text)
    text_compact = compact(text)
    terms = row_terms(row)
    matched_exact: list[str] = []
    matched_support: list[str] = []
    score = 0.0

    duty_base = base_duty_name(row["duty_name"])
    if duty_base and compact(duty_base) in text_compact:
        score += 10.0
        matched_exact.append(duty_base)
    duty_name = normalize_text(row["duty_name"])
    if duty_name and duty_name != duty_base and compact(duty_name) in text_compact:
        score += 4.0
        matched_exact.append(duty_name)
    job_name = normalize_text(row["job_name"])
    if job_name and compact(job_name) in text_compact:
        score += 5.0
        matched_exact.append(job_name)
    for field_name in [
        normalize_text(row["sqf_field_name"]),
        normalize_text(row["sqf_sub_field_name"]),
        normalize_text(row["sector_name"]),
    ]:
        if field_name and compact(field_name) in text_compact:
            score += 2.5
            matched_exact.append(field_name)

    level = row["sqf_level"]
    try:
        parsed_level = int(level) if level is not None else None
    except (TypeError, ValueError):
        parsed_level = None
    if level_matched(text, parsed_level):
        score += 2.0
        matched_exact.append(f"L{parsed_level}")

    for token in terms["support"]:
        if compact(token) in text_compact:
            score += 0.5
            matched_support.append(token)

    matched_exact = list(dict.fromkeys(matched_exact))
    matched_support = list(dict.fromkeys(matched_support))
    if score >= 15:
        relation = "strongEvidence"
    elif score >= 9:
        relation = "supportingEvidence"
    else:
        relation = "weakEvidence"
    return {
        "score": round(score, 2),
        "relation": relation,
        "matched_terms": {
            "exact": matched_exact,
            "support": matched_support[:20],
        },
        "evidence_text": snippet_around(text, matched_exact + matched_support),
    }


def load_job_levels(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            jl.sqf_job_level_id, jl.sqf_source_key, jl.duty_name,
            jl.sqf_level, jl.level_name, jl.job_level_definition,
            jl.duty_definition, jl.autonomy_responsibility,
            j.job_name, j.job_definition,
            s.sector_id, s.sector_name, s.ncs_lclas_cd, s.ncs_lclas_name,
            s.sqf_field_name, s.sqf_sub_field_name
        FROM sqf_job_levels_normalized jl
        JOIN sqf_jobs_normalized j ON j.sqf_job_id = jl.sqf_job_id
        JOIN sqf_industry_sectors s ON s.sector_id = j.sector_id
        ORDER BY jl.sqf_job_level_id
        """
    ).fetchall()


def build_token_index(rows: list[sqlite3.Row]) -> dict[str, set[int]]:
    index: dict[str, set[int]] = defaultdict(set)
    for row_index, row in enumerate(rows):
        terms = row_terms(row)
        for term in terms["exact"] + terms["support"][:8]:
            for token in tokenize(term):
                index[token].add(row_index)
            compact_term = compact(term)
            if len(compact_term) >= 3:
                index[compact_term].add(row_index)
    return index


def candidate_indexes_for_chunk(
    chunk_text: str,
    token_index: dict[str, set[int]],
    rows: list[sqlite3.Row],
    *,
    max_candidates: int = 300,
) -> list[int]:
    text = normalize_text(chunk_text)
    text_compact = compact(text)
    candidates: set[int] = set()
    for token in tokenize(text):
        candidates.update(token_index.get(token, set()))
        if len(candidates) >= max_candidates:
            break
    if len(candidates) < max_candidates:
        for term, row_indexes in token_index.items():
            if len(term) >= 4 and term in text_compact:
                candidates.update(row_indexes)
                if len(candidates) >= max_candidates:
                    break
    return sorted(candidates)


def fetch_chunks(
    conn: sqlite3.Connection,
    limit_chunks: int | None = None,
    asset_id: int | None = None,
    include_framework_references: bool = False,
) -> list[sqlite3.Row]:
    sql = """
        SELECT dc.chunk_id, dc.text, ds.ontology_role
        FROM sqf_document_chunks dc
        JOIN sqf_document_assets da ON da.asset_id = dc.asset_id
        JOIN sqf_document_sources ds ON ds.document_id = da.document_id
        WHERE dc.text IS NOT NULL AND TRIM(dc.text) != ''
    """
    params: list[Any] = []
    if asset_id is not None:
        sql += " AND dc.asset_id = ?"
        params.append(asset_id)
    if not include_framework_references:
        sql += " AND COALESCE(ds.ontology_role, '') != 'framework_reference'"
    sql += " ORDER BY chunk_id"
    if limit_chunks is not None:
        sql += " LIMIT ?"
        params.append(limit_chunks)
    return conn.execute(sql, params).fetchall()


def build_sqf_chunk_job_level_matches(
    db_path: Path,
    *,
    min_score: float = 9.0,
    max_matches_per_chunk: int = 8,
    limit_chunks: int | None = None,
    asset_id: int | None = None,
    reset: bool = True,
    include_framework_references: bool = False,
) -> dict[str, Any]:
    conn = connect(db_path)
    initialize_database(conn)
    create_indexes(conn)
    timestamp = now_utc()
    inserted = 0
    scored = 0
    try:
        if reset and asset_id is None:
            conn.execute("DELETE FROM sqf_chunk_job_level_matches")
        elif reset and asset_id is not None:
            conn.execute(
                """
                DELETE FROM sqf_chunk_job_level_matches
                WHERE chunk_id IN (
                    SELECT chunk_id
                    FROM sqf_document_chunks
                    WHERE asset_id = ?
                )
                """,
                (asset_id,),
            )
        job_levels = load_job_levels(conn)
        chunks = fetch_chunks(
            conn,
            limit_chunks,
            asset_id,
            include_framework_references=include_framework_references,
        )
        token_index = build_token_index(job_levels)
        for chunk in chunks:
            ranked: list[tuple[float, sqlite3.Row, dict[str, Any]]] = []
            for row_index in candidate_indexes_for_chunk(chunk["text"], token_index, job_levels):
                row = job_levels[row_index]
                score = score_chunk_for_job_level(chunk["text"], row)
                scored += 1
                if score["score"] >= min_score:
                    ranked.append((float(score["score"]), row, score))
            ranked.sort(key=lambda item: item[0], reverse=True)
            for _, row, score in ranked[:max_matches_per_chunk]:
                conn.execute(
                    """
                    INSERT INTO sqf_chunk_job_level_matches(
                        chunk_id, sqf_job_level_id, sqf_source_key, relation,
                        score, method, evidence_text, matched_terms_json,
                        review_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?)
                    ON CONFLICT(chunk_id, sqf_job_level_id, method) DO UPDATE SET
                        sqf_source_key = excluded.sqf_source_key,
                        relation = excluded.relation,
                        score = excluded.score,
                        evidence_text = excluded.evidence_text,
                        matched_terms_json = excluded.matched_terms_json,
                        review_status = excluded.review_status
                    """,
                    (
                        chunk["chunk_id"],
                        row["sqf_job_level_id"],
                        row["sqf_source_key"],
                        score["relation"],
                        score["score"],
                        "pdf_chunk_lexical_precision_v1",
                        score["evidence_text"],
                        json.dumps(score["matched_terms"], ensure_ascii=False),
                        timestamp,
                    ),
                )
                inserted += 1
            if inserted and inserted % 5000 == 0:
                conn.commit()
        conn.commit()
        relation_counts = rows_to_dicts(
            conn.execute(
                """
                SELECT relation, COUNT(*) AS count
                FROM sqf_chunk_job_level_matches
                GROUP BY relation
                ORDER BY count DESC
                """
            ).fetchall()
        )
        return {
            "chunks_seen": len(chunks),
            "job_levels_seen": len(job_levels),
            "candidate_pairs_scored": scored,
            "matches_inserted": inserted,
            "min_score": min_score,
            "max_matches_per_chunk": max_matches_per_chunk,
            "asset_id": asset_id,
            "include_framework_references": include_framework_references,
            "relation_counts": relation_counts,
            "note": "Matches are candidate document evidence, not official recognition decisions.",
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build SQF PDF chunk to job-level precision matches.")
    parser.add_argument("--db", type=Path, default=Path("data/processed/ncs.db"))
    parser.add_argument("--min-score", type=float, default=9.0)
    parser.add_argument("--max-matches-per-chunk", type=int, default=8)
    parser.add_argument("--limit-chunks", type=int)
    parser.add_argument("--asset-id", type=int)
    parser.add_argument("--no-reset", action="store_true")
    parser.add_argument("--include-framework-references", action="store_true")
    args = parser.parse_args()
    result = build_sqf_chunk_job_level_matches(
        args.db,
        min_score=args.min_score,
        max_matches_per_chunk=args.max_matches_per_chunk,
        limit_chunks=args.limit_chunks,
        asset_id=args.asset_id,
        reset=not args.no_reset,
        include_framework_references=args.include_framework_references,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
