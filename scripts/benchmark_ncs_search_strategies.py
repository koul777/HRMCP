from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import sqlite3
import statistics
import sys
import time
import unicodedata
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

SCHEMA = "ncs_search_strategy_experiments_v1"
DEFAULT_DB = ROOT / "data" / "processed" / "ncs.db"
DEFAULT_MANIFEST = (
    ROOT / "deploy" / "vercel_mcp_app" / "api" / "ncs_ontology_compact.manifest.json"
)
DEFAULT_OUT = ROOT / "reports" / "ncs_search_strategy_experiments_20260830.json"
DEFAULT_MARKDOWN_OUT = ROOT / "reports" / "ncs_search_strategy_experiments_20260830.md"
MAX_COMPACT_PROMOTION_BYTES = 460_000_000
MAX_RSS_DELTA_BYTES = 50_000_000
MIN_P50_IMPROVEMENT_RATIO = 0.25
MIN_PROXY_DELTA = -0.01
SEARCH_TYPES = ("unit", "element", "criteria", "ksa")
TIERS = ("phrase", "token_and", "token_or")
DEFAULT_QUERY_CASES = (
    {"query": "\ucc44\uc6a9", "case": "short_hr"},
    {"query": "\uc2e0\uc785\uc0ac\uc6d0 \ucc44\uc6a9 \uba74\uc811", "case": "multiword_hr"},
    {"query": "\ub370\uc774\ud130 \ubd84\uc11d\uac00", "case": "multiword_role"},
    {"query": "\ud488\uc9c8\uad00\ub9ac \ub2f4\ub2f9\uc790 \uad50\uc721", "case": "multiword_training"},
    {"query": "\ub178\ubb34\uad00\ub9ac", "case": "exact_unit"},
    {"query": "\uc778\uc0ac\uae30\ud68d", "case": "exact_unit"},
    {"query": "\uad50\uc721\ud6c8\ub828", "case": "compact_phrase"},
    {"query": "\ud68c\uacc4 \uac10\uc0ac", "case": "multiword_domain"},
    {"query": "\uc0b0\uc5c5 \uc548\uc804 \uad00\ub9ac", "case": "multiword_domain"},
    {"query": "\uace0\uac1d \uc11c\ube44\uc2a4", "case": "multiword_domain"},
    {"query": "\uae30\uacc4 \uc124\uacc4", "case": "multiword_domain"},
    {"query": "\uc18c\ud504\ud2b8\uc6e8\uc5b4 \uac1c\ubc1c", "case": "multiword_domain"},
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _percentile(values: Iterable[float], percentile_value: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    rank = max(1, math.ceil((percentile_value / 100.0) * len(ordered)))
    return round(ordered[rank - 1], 3)


def _latency(values: list[float]) -> dict[str, Any]:
    rounded = [round(value, 3) for value in values]
    return {
        "samples": rounded,
        "p50": _percentile(rounded, 50),
        "p95": _percentile(rounded, 95),
        "min": round(min(rounded), 3) if rounded else None,
        "max": round(max(rounded), 3) if rounded else None,
    }


def _normalize_query(query: str) -> tuple[str, list[str]]:
    normalized = unicodedata.normalize("NFKC", str(query or "")).casefold()
    normalized = re.sub(r"[^0-9a-z_\uac00-\ud7a3]+", " ", normalized)
    phrase = " ".join(normalized.split())
    tokens: list[str] = []
    for token in phrase.split():
        if token not in tokens:
            tokens.append(token)
        if len(tokens) == 4:
            break
    return phrase, tokens


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _rss_bytes() -> int | None:
    try:
        import psutil

        return int(psutil.Process(os.getpid()).memory_info().rss)
    except (ImportError, OSError, ValueError):
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            process = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                process, ctypes.byref(counters), counters.cb
            )
            return int(counters.WorkingSetSize) if ok else None
        except (AttributeError, OSError, ValueError):
            return None
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        resident = int(Path("/proc/self/statm").read_text().split()[1])
        return resident * int(page_size)
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _tier_predicate(columns: tuple[str, ...], tier: str, phrase: str, tokens: list[str]) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {
        "phrase_pattern": f"%{_escape_like(phrase)}%",
    }
    phrase_clause = " OR ".join(
        f"COALESCE({column}, '') LIKE :phrase_pattern ESCAPE '\\'" for column in columns
    )
    if tier == "phrase":
        return f"({phrase_clause})", params
    token_clauses: list[str] = []
    for index, token in enumerate(tokens):
        key = f"token_{index}"
        params[key] = f"%{_escape_like(token)}%"
        token_clauses.append(
            "(" + " OR ".join(
                f"COALESCE({column}, '') LIKE :{key} ESCAPE '\\'" for column in columns
            ) + ")"
        )
    if not token_clauses:
        return "0", params
    joiner = " AND " if tier == "token_and" else " OR "
    return "(" + joiner.join(token_clauses) + ")", params


def _raw_query_parts(item_type: str) -> tuple[tuple[str, ...], str, str, str]:
    if item_type == "unit":
        columns = (
            "cu.unit_code", "cu.unit_name_raw", "cu.api_definition",
            "c.major_name", "c.middle_name", "c.small_name", "c.sub_name",
            "aliases.alias_search_text",
        )
        prefix = """
            WITH alias_search AS (
                SELECT unit_code,
                       GROUP_CONCAT(COALESCE(alias_text, '') || ' ' ||
                                    COALESCE(normalized_query, ''), ' ') AS alias_search_text
                FROM ncs_query_aliases
                WHERE unit_code IS NOT NULL
                GROUP BY unit_code
            )
        """
        select_from = """
            SELECT cu.unit_code AS item_id, cu.unit_name_raw AS item_text,
                   cu.unit_name_raw, cu.api_definition,
                   c.major_name, c.middle_name, c.small_name, c.sub_name,
                   aliases.alias_search_text
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            LEFT JOIN alias_search aliases ON aliases.unit_code = cu.unit_code
        """
        order_by = """
            CASE
                WHEN cu.unit_code = :exact THEN 0
                WHEN TRIM(cu.unit_name_raw) = TRIM(:exact) COLLATE NOCASE THEN 0
                WHEN cu.unit_name_raw LIKE :prefix_pattern ESCAPE '\\' THEN 1
                WHEN cu.unit_name_raw LIKE :phrase_pattern ESCAPE '\\' THEN 2
                WHEN c.major_name LIKE :phrase_pattern ESCAPE '\\'
                  OR c.middle_name LIKE :phrase_pattern ESCAPE '\\'
                  OR c.small_name LIKE :phrase_pattern ESCAPE '\\'
                  OR c.sub_name LIKE :phrase_pattern ESCAPE '\\' THEN 3
                WHEN cu.api_definition LIKE :phrase_pattern ESCAPE '\\' THEN 4
                ELSE 5
            END, LENGTH(cu.unit_name_raw), cu.unit_code
        """
        return columns, prefix, select_from, order_by
    if item_type == "element":
        return (
            ("ce.element_name_raw",),
            "",
            """
                SELECT ce.element_id AS item_id, ce.element_name_raw AS item_text,
                       cu.unit_name_raw, ce.element_name_raw,
                       NULL AS criteria_text_raw, NULL AS ksa_text_raw
                FROM competency_elements ce
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
            """,
            "LENGTH(ce.element_name_raw), ce.element_id",
        )
    if item_type == "criteria":
        return (
            ("pc.criteria_text_raw", "pc.criteria_text_refined"),
            "",
            """
                SELECT pc.criteria_id AS item_id, pc.criteria_text_raw AS item_text,
                       cu.unit_name_raw, ce.element_name_raw,
                       pc.criteria_text_raw, NULL AS ksa_text_raw
                FROM performance_criteria pc
                JOIN competency_elements ce ON ce.element_id = pc.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
            """,
            "pc.criteria_id",
        )
    if item_type == "ksa":
        return (
            ("ki.ksa_text_raw", "ki.ksa_text_refined"),
            "",
            """
                SELECT ki.ksa_id AS item_id, ki.ksa_text_raw AS item_text,
                       cu.unit_name_raw, ce.element_name_raw,
                       NULL AS criteria_text_raw, ki.ksa_text_raw
                FROM ksa_items ki
                JOIN competency_elements ce ON ce.element_id = ki.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
            """,
            "ki.ksa_id",
        )
    raise ValueError(f"unsupported item type: {item_type}")


def _row_result(item_type: str, row: sqlite3.Row, mode: str) -> dict[str, Any]:
    fields = [
        row[key]
        for key in row.keys()
        if key not in {"item_id"} and isinstance(row[key], str)
    ]
    return {
        "type": item_type,
        "id": row["item_id"],
        "text": row["item_text"],
        "match_mode": mode,
        "search_blob": " ".join(fields),
    }


def _fetch_raw_tier(
    conn: sqlite3.Connection,
    item_type: str,
    phrase: str,
    tokens: list[str],
    tier: str,
    candidate_limit: int,
) -> list[dict[str, Any]]:
    columns, prefix, select_from, order_by = _raw_query_parts(item_type)
    where_clause, params = _tier_predicate(columns, tier, phrase, tokens)
    params.update(
        {
            "candidate_limit": candidate_limit,
            "exact": phrase,
            "prefix_pattern": f"{_escape_like(phrase)}%",
        }
    )
    sql = f"{prefix} {select_from} WHERE {where_clause} ORDER BY {order_by} LIMIT :candidate_limit"
    return [_row_result(item_type, row, tier) for row in conn.execute(sql, params)]


def _fetch_lazy_type(
    conn: sqlite3.Connection,
    item_type: str,
    phrase: str,
    tokens: list[str],
    candidate_limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    executed: list[str] = []
    tier_rows: dict[str, int] = {}
    for tier in TIERS:
        if tier != "phrase" and len(tokens) < 2:
            continue
        executed.append(tier)
        rows = _fetch_raw_tier(conn, item_type, phrase, tokens, tier, candidate_limit)
        tier_rows[tier] = len(rows)
        if rows:
            return rows, {
                "selected_tier": tier,
                "tiers_executed": executed,
                "tier_row_counts": tier_rows,
                "early_exit": tier != "token_or",
            }
    return [], {
        "selected_tier": None,
        "tiers_executed": executed,
        "tier_row_counts": tier_rows,
        "early_exit": False,
    }


def _round_robin(candidates: dict[str, list[dict[str, Any]]], requested: tuple[str, ...]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index = 0
    while True:
        appended = False
        for item_type in requested:
            rows = candidates.get(item_type, [])
            if index < len(rows):
                merged.append(rows[index])
                appended = True
        if not appended:
            return merged
        index += 1


def lazy_tier_search(
    db_path: Path,
    query: str,
    scope: str = "all",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    phrase, tokens = _normalize_query(query)
    requested = SEARCH_TYPES if scope == "all" else (scope,)
    candidate_limit = max(1, offset + limit + 1)
    candidates: dict[str, list[dict[str, Any]]] = {}
    stats: dict[str, Any] = {}
    with closing(_open_read_only(db_path)) as conn:
        for item_type in requested:
            candidates[item_type], stats[item_type] = _fetch_lazy_type(
                conn, item_type, phrase, tokens, candidate_limit
            )
    merged = _round_robin(candidates, requested)
    page = merged[offset : offset + limit]
    return {
        "strategy": "lazy_tier",
        "query": query,
        "normalized_query": phrase,
        "query_tokens": tokens,
        "scope": scope,
        "results": page,
        "counts_by_type": dict(Counter(row["type"] for row in page)),
        "instrumentation": {"raw_by_type": stats},
    }


def _concept_fanout(conn: sqlite3.Connection, concept_id: int) -> int:
    ksa = conn.execute(
        "SELECT COUNT(*) FROM ksa_concept_links WHERE concept_id=?", (concept_id,)
    ).fetchone()[0]
    criteria = conn.execute(
        "SELECT COUNT(*) FROM criteria_concept_links WHERE concept_id=?", (concept_id,)
    ).fetchone()[0]
    return int(ksa) + int(criteria)


def _resolve_concepts(
    conn: sqlite3.Connection,
    tokens: list[str],
    *,
    max_per_token: int,
    max_total: int,
    max_fanout: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[int] = set()
    started = time.perf_counter()
    for token_index, token in enumerate(tokens):
        candidates: dict[int, dict[str, Any]] = {}
        probes = (
            (
                "concept",
                "SELECT concept_id, concept_name AS label, normalized_key AS key "
                "FROM ontology_concepts WHERE normalized_key GLOB ? "
                "ORDER BY LENGTH(normalized_key), concept_id LIMIT ?",
            ),
            (
                "alias",
                "SELECT concept_id, alias_text AS label, normalized_alias_key AS key "
                "FROM ontology_concept_aliases WHERE normalized_alias_key GLOB ? "
                "ORDER BY LENGTH(normalized_alias_key), concept_id LIMIT ?",
            ),
        )
        for source, sql in probes:
            for row in conn.execute(sql, (token + "*", max_per_token * 3)):
                concept_id = int(row["concept_id"])
                key = str(row["key"] or "")
                candidate = {
                    "concept_id": concept_id,
                    "label": row["label"],
                    "key": key,
                    "token": token,
                    "token_index": token_index,
                    "match_rank": 0 if key == token else 1,
                    "source": source,
                }
                previous = candidates.get(concept_id)
                if previous is None or (candidate["match_rank"], len(key), source) < (
                    previous["match_rank"], len(previous["key"]), previous["source"]
                ):
                    candidates[concept_id] = candidate
        ranked = sorted(
            candidates.values(),
            key=lambda item: (item["match_rank"], len(item["key"]), item["concept_id"]),
        )
        token_added = 0
        for candidate in ranked:
            if candidate["concept_id"] in seen:
                continue
            fanout = _concept_fanout(conn, candidate["concept_id"])
            candidate["fanout"] = fanout
            if fanout > max_fanout:
                skipped.append({**candidate, "reason": "fanout_cap"})
                continue
            accepted.append(candidate)
            seen.add(candidate["concept_id"])
            token_added += 1
            if token_added >= max_per_token or len(accepted) >= max_total:
                break
        if len(accepted) >= max_total:
            break
    return accepted, {
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "accepted_count": len(accepted),
        "skipped_count": len(skipped),
        "skipped_preview": skipped[:10],
        "max_fanout": max_fanout,
    }


def _ranked_values(concepts: list[dict[str, Any]]) -> tuple[str, list[Any]]:
    values = ",".join("(?,?)" for _ in concepts)
    params: list[Any] = []
    for rank, concept in enumerate(concepts):
        params.extend((concept["concept_id"], rank))
    return values, params


def _fetch_concept_type(
    conn: sqlite3.Connection,
    item_type: str,
    concepts: list[dict[str, Any]],
    candidate_limit: int,
) -> list[dict[str, Any]]:
    if not concepts:
        return []
    values, params = _ranked_values(concepts)
    if item_type == "ksa":
        sql = f"""
            WITH ranked(concept_id, concept_rank) AS (VALUES {values})
            SELECT ki.ksa_id AS item_id, ki.ksa_text_raw AS item_text,
                   cu.unit_name_raw, ce.element_name_raw,
                   NULL AS criteria_text_raw, ki.ksa_text_raw,
                   MIN(r.concept_rank) AS evidence_rank
            FROM ranked r
            JOIN ksa_concept_links kcl ON kcl.concept_id=r.concept_id
            JOIN ksa_items ki ON ki.ksa_id=kcl.ksa_id
            JOIN competency_elements ce ON ce.element_id=ki.element_id
            JOIN competency_units cu ON cu.unit_code=ce.unit_code
            GROUP BY ki.ksa_id
            ORDER BY evidence_rank, ki.ksa_id
            LIMIT ?
        """
    elif item_type == "criteria":
        sql = f"""
            WITH ranked(concept_id, concept_rank) AS (VALUES {values})
            SELECT pc.criteria_id AS item_id, pc.criteria_text_raw AS item_text,
                   cu.unit_name_raw, ce.element_name_raw,
                   pc.criteria_text_raw, NULL AS ksa_text_raw,
                   MIN(r.concept_rank) AS evidence_rank
            FROM ranked r
            JOIN criteria_concept_links ccl ON ccl.concept_id=r.concept_id
            JOIN performance_criteria pc ON pc.criteria_id=ccl.criteria_id
            JOIN competency_elements ce ON ce.element_id=pc.element_id
            JOIN competency_units cu ON cu.unit_code=ce.unit_code
            GROUP BY pc.criteria_id
            ORDER BY evidence_rank, pc.criteria_id
            LIMIT ?
        """
    elif item_type == "element":
        sql = f"""
            WITH ranked(concept_id, concept_rank) AS (VALUES {values}),
            evidence AS (
                SELECT ce.element_id, ce.element_name_raw, cu.unit_name_raw,
                       r.concept_rank
                FROM ranked r
                JOIN ksa_concept_links kcl ON kcl.concept_id=r.concept_id
                JOIN ksa_items ki ON ki.ksa_id=kcl.ksa_id
                JOIN competency_elements ce ON ce.element_id=ki.element_id
                JOIN competency_units cu ON cu.unit_code=ce.unit_code
                UNION ALL
                SELECT ce.element_id, ce.element_name_raw, cu.unit_name_raw,
                       r.concept_rank
                FROM ranked r
                JOIN criteria_concept_links ccl ON ccl.concept_id=r.concept_id
                JOIN performance_criteria pc ON pc.criteria_id=ccl.criteria_id
                JOIN competency_elements ce ON ce.element_id=pc.element_id
                JOIN competency_units cu ON cu.unit_code=ce.unit_code
            )
            SELECT element_id AS item_id, element_name_raw AS item_text,
                   unit_name_raw, element_name_raw,
                   NULL AS criteria_text_raw, NULL AS ksa_text_raw,
                   MIN(concept_rank) AS evidence_rank
            FROM evidence
            GROUP BY element_id
            ORDER BY evidence_rank, element_id
            LIMIT ?
        """
    elif item_type == "unit":
        sql = f"""
            WITH ranked(concept_id, concept_rank) AS (VALUES {values}),
            evidence AS (
                SELECT cu.unit_code, cu.unit_name_raw, r.concept_rank
                FROM ranked r
                JOIN ksa_concept_links kcl ON kcl.concept_id=r.concept_id
                JOIN ksa_items ki ON ki.ksa_id=kcl.ksa_id
                JOIN competency_elements ce ON ce.element_id=ki.element_id
                JOIN competency_units cu ON cu.unit_code=ce.unit_code
                UNION ALL
                SELECT cu.unit_code, cu.unit_name_raw, r.concept_rank
                FROM ranked r
                JOIN criteria_concept_links ccl ON ccl.concept_id=r.concept_id
                JOIN performance_criteria pc ON pc.criteria_id=ccl.criteria_id
                JOIN competency_elements ce ON ce.element_id=pc.element_id
                JOIN competency_units cu ON cu.unit_code=ce.unit_code
            )
            SELECT unit_code AS item_id, unit_name_raw AS item_text,
                   unit_name_raw, NULL AS element_name_raw,
                   NULL AS criteria_text_raw, NULL AS ksa_text_raw,
                   MIN(concept_rank) AS evidence_rank
            FROM evidence
            GROUP BY unit_code
            ORDER BY evidence_rank, unit_code
            LIMIT ?
        """
    else:
        raise ValueError(f"unsupported item type: {item_type}")
    rows = conn.execute(sql, [*params, candidate_limit]).fetchall()
    return [_row_result(item_type, row, "concept_link") for row in rows]


def concept_first_search(
    db_path: Path,
    query: str,
    scope: str = "all",
    limit: int = 20,
    offset: int = 0,
    *,
    max_concept_fanout: int = 20_000,
) -> dict[str, Any]:
    phrase, tokens = _normalize_query(query)
    requested = SEARCH_TYPES if scope == "all" else (scope,)
    candidate_limit = max(1, offset + limit + 1)
    candidates: dict[str, list[dict[str, Any]]] = {}
    raw_stats: dict[str, Any] = {}
    with closing(_open_read_only(db_path)) as conn:
        concepts, resolver_stats = _resolve_concepts(
            conn,
            tokens,
            max_per_token=4,
            max_total=12,
            max_fanout=max_concept_fanout,
        )
        evidence_started = time.perf_counter()
        for item_type in requested:
            concept_rows = _fetch_concept_type(
                conn, item_type, concepts, candidate_limit
            )
            rows = list(concept_rows)
            fallback_used = len(rows) < candidate_limit
            if fallback_used:
                raw_rows, raw_stat = _fetch_lazy_type(
                    conn, item_type, phrase, tokens, candidate_limit
                )
                seen = {(row["type"], str(row["id"])) for row in rows}
                rows.extend(
                    row
                    for row in raw_rows
                    if (row["type"], str(row["id"])) not in seen
                )
                raw_stats[item_type] = raw_stat
            candidates[item_type] = rows[:candidate_limit]
            raw_stats.setdefault(item_type, {"selected_tier": None, "tiers_executed": []})
            raw_stats[item_type]["fallback_used"] = fallback_used
            raw_stats[item_type]["concept_result_count"] = len(concept_rows)
        evidence_ms = round((time.perf_counter() - evidence_started) * 1000, 3)
    merged = _round_robin(candidates, requested)
    page = merged[offset : offset + limit]
    return {
        "strategy": "concept_first",
        "query": query,
        "normalized_query": phrase,
        "query_tokens": tokens,
        "scope": scope,
        "results": page,
        "counts_by_type": dict(Counter(row["type"] for row in page)),
        "instrumentation": {
            "resolver": resolver_stats,
            "accepted_concepts": concepts,
            "evidence_and_fallback_ms": evidence_ms,
            "raw_by_type": raw_stats,
        },
    }


def _adapt_runtime_payload(payload: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for row in payload.get("results", []):
        path = row.get("path") if isinstance(row.get("path"), dict) else {}
        blob_parts = [
            str(row.get("text") or ""),
            str(row.get("api_definition") or ""),
        ]
        blob_parts.extend(str(value or "") for value in path.values())
        results.append(
            {
                "type": row.get("type"),
                "id": row.get("id"),
                "text": row.get("text"),
                "match_mode": row.get("match_mode"),
                "search_blob": " ".join(blob_parts),
            }
        )
    return {
        "strategy": "p1_baseline",
        "query": payload.get("query"),
        "normalized_query": payload.get("normalized_query"),
        "query_tokens": payload.get("query_tokens", []),
        "scope": payload.get("scope"),
        "results": results,
        "counts_by_type": payload.get("counts_by_type", {}),
        "instrumentation": {"match_mode_by_type": payload.get("match_mode_by_type", {})},
    }


def _result_ids(payload: dict[str, Any], limit: int | None = None) -> list[str]:
    rows = payload.get("results", [])
    if limit is not None:
        rows = rows[:limit]
    return [f"{row.get('type')}:{row.get('id')}" for row in rows]


def _quality_proxy(payload: dict[str, Any], baseline_ids: list[str]) -> dict[str, Any]:
    rows = payload.get("results", [])
    _phrase, tokens = _normalize_query(str(payload.get("query") or ""))
    row_coverages: list[float] = []
    any_token_hits = 0
    all_token_hits = 0
    for row in rows:
        blob, _ = _normalize_query(str(row.get("search_blob") or row.get("text") or ""))
        hits = sum(1 for token in tokens if token in blob)
        coverage = hits / len(tokens) if tokens else 0.0
        row_coverages.append(coverage)
        any_token_hits += int(hits > 0)
        all_token_hits += int(bool(tokens) and hits == len(tokens))
    ids = _result_ids(payload, 10)
    baseline_set = set(baseline_ids[:10])
    overlap = len(set(ids) & baseline_set) / max(1, min(10, len(baseline_ids)))
    return {
        "mean_query_token_coverage": round(statistics.fmean(row_coverages), 4) if row_coverages else 0.0,
        "any_token_hit_rate": round(any_token_hits / len(rows), 4) if rows else 0.0,
        "all_token_hit_rate": round(all_token_hits / len(rows), 4) if rows else 0.0,
        "baseline_overlap_at_10": round(overlap, 4),
        "distinct_type_count": len({row.get("type") for row in rows}),
        "labeled_recall_available": False,
    }


Strategy = Callable[[str, int, int], dict[str, Any]]


def benchmark(
    strategies: dict[str, Strategy],
    query_cases: list[dict[str, str]],
    *,
    runs: int,
    limit: int,
) -> list[dict[str, Any]]:
    samples: dict[tuple[str, str], list[dict[str, Any]]] = {
        (case["query"], name): [] for case in query_cases for name in strategies
    }
    for case_index, case in enumerate(query_cases):
        query = case["query"]
        names = list(strategies)
        for run_index in range(runs):
            rotated = names[(case_index + run_index) % len(names) :] + names[: (case_index + run_index) % len(names)]
            for name in rotated:
                rss_before = _rss_bytes()
                started = time.perf_counter()
                payload = strategies[name](query, limit, 0)
                elapsed_ms = (time.perf_counter() - started) * 1000
                rss_after = _rss_bytes()
                samples[(query, name)].append(
                    {
                        "elapsed_ms": elapsed_ms,
                        "rss_before": rss_before,
                        "rss_after": rss_after,
                        "payload": payload,
                    }
                )
    records: list[dict[str, Any]] = []
    for case in query_cases:
        query = case["query"]
        baseline_payload = samples[(query, "p1_baseline")][-1]["payload"]
        baseline_ids = _result_ids(baseline_payload, 10)
        for name in strategies:
            values = samples[(query, name)]
            payload = values[-1]["payload"]
            rss_values = [item["rss_after"] for item in values if item["rss_after"] is not None]
            records.append(
                {
                    "query": query,
                    "case": case["case"],
                    "strategy": name,
                    "elapsed_ms": _latency([item["elapsed_ms"] for item in values]),
                    "rss_after_bytes": max(rss_values) if rss_values else None,
                    "result_count": len(payload.get("results", [])),
                    "zero_hit": not payload.get("results"),
                    "counts_by_type": payload.get("counts_by_type", {}),
                    "stable_ids": len({_result_ids(item["payload"]).__repr__() for item in values}) == 1,
                    "preview_ids": _result_ids(payload, 5),
                    "quality_proxy": _quality_proxy(payload, baseline_ids),
                    "instrumentation": payload.get("instrumentation", {}),
                }
            )
    return records


def _aggregate(records: list[dict[str, Any]], compact_bytes: int | None) -> dict[str, Any]:
    by_strategy: dict[str, dict[str, Any]] = {}
    for name in ("p1_baseline", "lazy_tier", "concept_first"):
        chosen = [record for record in records if record["strategy"] == name]
        p50_values = [float(record["elapsed_ms"]["p50"]) for record in chosen]
        p95_values = [float(record["elapsed_ms"]["p95"]) for record in chosen]
        rss = [record["rss_after_bytes"] for record in chosen if record["rss_after_bytes"] is not None]
        proxy = [float(record["quality_proxy"]["mean_query_token_coverage"]) for record in chosen]
        overlap = [float(record["quality_proxy"]["baseline_overlap_at_10"]) for record in chosen]
        by_strategy[name] = {
            "query_count": len(chosen),
            "zero_hit_count": sum(int(record["zero_hit"]) for record in chosen),
            "aggregate_p50_ms": round(statistics.median(p50_values), 3),
            "aggregate_p95_ms": round(_percentile(p95_values, 95) or 0.0, 3),
            "max_rss_bytes": max(rss) if rss else None,
            "mean_query_token_coverage": round(statistics.fmean(proxy), 4),
            "mean_baseline_overlap_at_10": round(statistics.fmean(overlap), 4),
            "all_deterministic": all(record["stable_ids"] for record in chosen),
            "mean_distinct_type_count": round(
                statistics.fmean(record["quality_proxy"]["distinct_type_count"] for record in chosen), 3
            ),
        }
    baseline = by_strategy["p1_baseline"]
    for name in ("lazy_tier", "concept_first"):
        current = by_strategy[name]
        current["p50_improvement_ratio"] = round(
            (baseline["aggregate_p50_ms"] - current["aggregate_p50_ms"])
            / baseline["aggregate_p50_ms"],
            4,
        )
        current["lexical_proxy_delta"] = round(
            current["mean_query_token_coverage"] - baseline["mean_query_token_coverage"], 4
        )
        current["max_rss_delta_bytes"] = (
            current["max_rss_bytes"] - baseline["max_rss_bytes"]
            if current["max_rss_bytes"] is not None and baseline["max_rss_bytes"] is not None
            else None
        )
        gates = {
            "warm_p50_improves_at_least_25pct": current["p50_improvement_ratio"] >= MIN_P50_IMPROVEMENT_RATIO,
            "lexical_proxy_delta_at_least_minus_0_01": current["lexical_proxy_delta"] >= MIN_PROXY_DELTA,
            "labeled_recall_delta_at_least_minus_0_01": None,
            "max_rss_delta_within_50mb": current["max_rss_delta_bytes"] is not None
            and current["max_rss_delta_bytes"] <= MAX_RSS_DELTA_BYTES,
            "compact_within_460mb": compact_bytes is not None and compact_bytes <= MAX_COMPACT_PROMOTION_BYTES,
            "deterministic_ordering": current["all_deterministic"],
            "baseline_top10_id_parity": current["mean_baseline_overlap_at_10"] >= 0.99,
            "pagination_contract": True,
            "new_index_bytes": 0,
        }
        current["promotion_gates"] = gates
        current["promotion_recommendation"] = (
            "hold_for_labeled_recall_and_product_parity"
            if all(value is not False for value in gates.values())
            else "do_not_promote"
        )
    return by_strategy


def _manifest_snapshot_bytes(path: Path) -> int | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = payload.get("sqlite_bytes")
    return int(value) if isinstance(value, int) else None


def _query_plan_evidence(db_path: Path) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    with closing(_open_read_only(db_path)) as conn:
        for name, sql in (
            (
                "concept_prefix",
                "SELECT concept_id FROM ontology_concepts WHERE normalized_key GLOB ? LIMIT 12",
            ),
            (
                "alias_prefix",
                "SELECT concept_id FROM ontology_concept_aliases WHERE normalized_alias_key GLOB ? LIMIT 12",
            ),
            (
                "ksa_link",
                "SELECT ksa_id FROM ksa_concept_links WHERE concept_id IN (?,?) LIMIT 21",
            ),
            (
                "criteria_link",
                "SELECT criteria_id FROM criteria_concept_links WHERE concept_id IN (?,?) LIMIT 21",
            ),
        ):
            params = ("\ucc44\uc6a9*",) if "prefix" in name else (1, 2)
            evidence[name] = [
                row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
            ]
    return evidence


def build_report(
    db_path: Path,
    manifest_path: Path,
    query_cases: list[dict[str, str]],
    *,
    runs: int,
    limit: int,
) -> dict[str, Any]:
    os.environ["NCS_DB_PATH"] = str(db_path.resolve())
    os.environ["NCS_MCP_READ_ONLY_MODE"] = "true"
    os.environ["NCS_MCP_OPERATOR_TOOLS"] = "false"
    from ncs_mcp.server import search_ncs

    strategies: dict[str, Strategy] = {
        "p1_baseline": lambda query, result_limit, offset: _adapt_runtime_payload(
            search_ncs(query, "all", result_limit, offset)
        ),
        "lazy_tier": lambda query, result_limit, offset: lazy_tier_search(
            db_path, query, "all", result_limit, offset
        ),
        "concept_first": lambda query, result_limit, offset: concept_first_search(
            db_path, query, "all", result_limit, offset
        ),
    }
    records = benchmark(strategies, query_cases, runs=runs, limit=limit)
    compact_bytes = _manifest_snapshot_bytes(manifest_path)
    aggregate = _aggregate(records, compact_bytes)
    latency_winner = min(
        aggregate, key=lambda name: aggregate[name]["aggregate_p50_ms"]
    )
    observed_candidates = [
        name
        for name in ("lazy_tier", "concept_first")
        if all(
            value is not False
            for key, value in aggregate[name]["promotion_gates"].items()
            if key != "labeled_recall_delta_at_least_minus_0_01"
        )
    ]
    return {
        "schema": SCHEMA,
        "version": 1,
        "generated_at": _now(),
        "mode": "read_only_strategy_experiment",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "latency_caveat": "Local warm filesystem/cache measurement; not Vercel latency.",
        },
        "database": {
            "path": str(db_path),
            "bytes": db_path.stat().st_size,
            "open_mode": "mode=ro&immutable=1",
            "writes": False,
        },
        "deployment_budget": {
            "compact_snapshot_bytes": compact_bytes,
            "promotion_max_bytes": MAX_COMPACT_PROMOTION_BYTES,
            "new_index_bytes": 0,
        },
        "experiment": {
            "runs_per_query": runs,
            "limit": limit,
            "query_count": len(query_cases),
            "records": records,
            "aggregate": aggregate,
            "query_plan_evidence": _query_plan_evidence(db_path),
        },
        "decision": {
            "winner": "none_promoted",
            "latency_winner": latency_winner,
            "observed_gate_candidate": observed_candidates[0] if observed_candidates else None,
            "promotion_rule": {
                "warm_p50_improvement_ratio": MIN_P50_IMPROVEMENT_RATIO,
                "minimum_recall_or_proxy_delta": MIN_PROXY_DELTA,
                "max_rss_delta_bytes": MAX_RSS_DELTA_BYTES,
                "max_compact_bytes": MAX_COMPACT_PROMOTION_BYTES,
                "deterministic_ordering_and_pagination_required": True,
            },
            "warning": (
                "Zero-hit reduction alone is not an improvement if token-OR noise or latency grows. "
                "Lexical coverage is only a proxy; no Recall@K, MRR, or nDCG promotion claim is allowed "
                "without a labeled evaluation pack."
            ),
        },
        "safety": {
            "database_writes": False,
            "schema_changes": False,
            "new_indexes": False,
            "raw_ksa_changes": False,
            "human_review_status_changes": False,
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    aggregate = report["experiment"]["aggregate"]
    lines = [
        "# NCS Search Strategy Experiments",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Database mode: `{report['database']['open_mode']}`",
        f"- Queries / runs: `{report['experiment']['query_count']}` / `{report['experiment']['runs_per_query']}`",
        "- Scope: `all`; limit: `20` unless overridden.",
        "- No DB, schema, index, raw KSA, or review-status mutation.",
        "",
        "## Aggregate",
        "",
        "| Strategy | p50 ms | p95 ms | p50 improve | zero-hit | lexical proxy | proxy delta | overlap@10 | max RSS delta | recommendation |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for name, item in aggregate.items():
        lines.append(
            "| " + " | ".join(
                [
                    name,
                    str(item["aggregate_p50_ms"]),
                    str(item["aggregate_p95_ms"]),
                    str(item.get("p50_improvement_ratio", "baseline")),
                    str(item["zero_hit_count"]),
                    str(item["mean_query_token_coverage"]),
                    str(item.get("lexical_proxy_delta", "baseline")),
                    str(item["mean_baseline_overlap_at_10"]),
                    str(item.get("max_rss_delta_bytes", "baseline")),
                    str(item.get("promotion_recommendation", "baseline")),
                ]
            ) + " |"
        )
    lines.extend(
        [
            "",
            "## Per-query p50",
            "",
            "| Query | baseline | lazy tier | concept first | lazy tier modes | concept fallback types |",
            "| --- | ---: | ---: | ---: | --- | --- |",
        ]
    )
    records = report["experiment"]["records"]
    queries = list(dict.fromkeys(record["query"] for record in records))
    for query in queries:
        row = {record["strategy"]: record for record in records if record["query"] == query}
        lazy_stats = row["lazy_tier"]["instrumentation"].get("raw_by_type", {})
        lazy_modes = ", ".join(
            f"{key}:{value.get('selected_tier')}" for key, value in lazy_stats.items()
        )
        concept_stats = row["concept_first"]["instrumentation"].get("raw_by_type", {})
        fallbacks = ", ".join(
            key for key, value in concept_stats.items() if value.get("fallback_used")
        ) or "none"
        lines.append(
            f"| {query} | {row['p1_baseline']['elapsed_ms']['p50']} | "
            f"{row['lazy_tier']['elapsed_ms']['p50']} | {row['concept_first']['elapsed_ms']['p50']} | "
            f"{lazy_modes} | {fallbacks} |"
        )
    lines.extend(
        [
            "",
            "## Promotion gates",
            "",
            "- Warm p50 must improve by at least `25%`.",
            "- Labeled recall delta must be at least `-0.01`; it is not available in this experiment.",
            "- Lexical token-coverage proxy delta must be at least `-0.01`, but cannot replace labeled recall.",
            "- Maximum RSS increase must be no more than `50 MB`.",
            "- Compact snapshot must be no more than `460 MB`; new index bytes here are `0`.",
            "- Deterministic ordering and pagination must remain intact.",
            "",
            "## Interpretation",
            "",
            f"- Winner by local aggregate p50 only: `{report['decision']['latency_winner']}`.",
            f"- Observed-gate candidate pending labeled recall: `{report['decision']['observed_gate_candidate']}`.",
            "- Promoted winner: `none`; experiment code does not change production search.",
            f"- {report['decision']['warning']}",
            "- Strategy A is semantically intended to preserve the first non-empty tier per type.",
            "- Strategy B uses indexed exact/prefix concept keys, caps fanout, then falls back to raw lazy tiers.",
            "- A production promotion still requires public-payload parity, labeled relevance, and remote warm/cold evidence.",
            "",
            "## Reproduce",
            "",
            "```powershell",
            "$env:PYTHONPATH='C:\\workspace\\NCS_MCP\\src'",
            "python scripts\\benchmark_ncs_search_strategies.py --runs 3 --limit 20",
            "python -m unittest tests.test_benchmark_ncs_search_strategies -v",
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare read-only NCS search strategies.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--query", action="append", dest="queries")
    args = parser.parse_args(argv)
    if not 1 <= args.runs <= 20:
        parser.error("--runs must be between 1 and 20")
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    query_cases = (
        [{"query": query, "case": "custom"} for query in args.queries]
        if args.queries
        else [dict(item) for item in DEFAULT_QUERY_CASES]
    )
    report = build_report(
        args.db.resolve(),
        args.manifest.resolve(),
        query_cases,
        runs=args.runs,
        limit=args.limit,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"aggregate": report["experiment"]["aggregate"], "decision": report["decision"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
