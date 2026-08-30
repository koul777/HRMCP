from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import sqlite3
import time
import unicodedata
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "processed" / "ncs.db"
DEFAULT_EVAL = ROOT / "reports" / "ncs_search_eval_candidates_20260830.json"
DEFAULT_OUT = ROOT / "reports" / "ncs_single_pass_search_experiment_20260830.json"
DEFAULT_MARKDOWN_OUT = ROOT / "reports" / "ncs_single_pass_search_experiment_20260830.md"
SCHEMA = "ncs_single_pass_search_experiment_v1"
SEARCH_TYPES = ("unit", "element", "criteria", "ksa")
MATCH_MODES = {0: "phrase", 1: "token_and", 2: "token_or"}
TOKEN_SPLIT_RE = re.compile(r"[^0-9A-Za-z가-힣_]+")


def generated_at() -> str:
    return datetime.now(UTC).isoformat()


def percentile(values: Iterable[float], percentile_value: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    rank = max(1, math.ceil((percentile_value / 100.0) * len(ordered)))
    return round(ordered[rank - 1], 3)


def latency_summary(values: list[float]) -> dict[str, Any]:
    rounded = [round(float(value), 3) for value in values]
    return {
        "samples_ms": rounded,
        "sample_count": len(rounded),
        "p50_ms": percentile(rounded, 50),
        "p95_ms": percentile(rounded, 95),
        "min_ms": round(min(rounded), 3) if rounded else None,
        "max_ms": round(max(rounded), 3) if rounded else None,
    }


def normalize_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(TOKEN_SPLIT_RE.sub(" ", normalized).split())


def normalize_query(query: str, max_tokens: int = 4) -> tuple[str, list[str]]:
    phrase = normalize_text(query)
    seen: set[str] = set()
    tokens: list[str] = []
    for token in phrase.split():
        folded = token.casefold()
        if folded and folded not in seen:
            seen.add(folded)
            tokens.append(token)
        if len(tokens) >= max_tokens:
            break
    return phrase, tokens


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def stable_ids(payload: dict[str, Any], limit: int = 10) -> list[str]:
    return [
        f"{item.get('type', 'unknown')}:{item.get('id')}"
        for item in payload.get("results", [])[:limit]
    ]


def current_rss_bytes() -> int | None:
    try:
        import psutil  # type: ignore

        return int(psutil.Process(os.getpid()).memory_info().rss)
    except (ImportError, OSError, AttributeError):
        return None


def _like_any(columns: tuple[str, ...], parameter: str) -> str:
    return "(" + " OR ".join(
        f"COALESCE({column}, '') LIKE :{parameter} ESCAPE '\\'" for column in columns
    ) + ")"


def _ranking_expressions(
    columns: tuple[str, ...],
    tokens: list[str],
) -> tuple[str, str, str, str]:
    token_matches = [_like_any(columns, f"token_{index}") for index in range(len(tokens))]
    any_match = "(" + " OR ".join(token_matches) + ")"
    all_match = "(" + " AND ".join(token_matches) + ")"
    matched_count = " + ".join(
        f"CASE WHEN {expression} THEN 1 ELSE 0 END" for expression in token_matches
    )
    phrase_match = _like_any(columns, "phrase_pattern")
    tier = f"CASE WHEN {phrase_match} THEN 0 WHEN {all_match} THEN 1 ELSE 2 END"
    return any_match, matched_count, phrase_match, tier


def _query_rows(
    conn: sqlite3.Connection,
    item_type: str,
    *,
    phrase: str,
    tokens: list[str],
    candidate_limit: int,
) -> list[sqlite3.Row]:
    if item_type == "unit":
        columns = (
            "cu.unit_code",
            "cu.unit_name_raw",
            "cu.api_definition",
            "c.major_name",
            "c.middle_name",
            "c.small_name",
            "c.sub_name",
            "aliases.alias_search_text",
        )
        any_match, matched_count, phrase_match, tier = _ranking_expressions(columns, tokens)
        sql = f"""
            WITH alias_search AS (
                SELECT unit_code,
                       GROUP_CONCAT(
                           COALESCE(alias_text, '') || ' ' || COALESCE(normalized_query, ''),
                           ' '
                       ) AS alias_search_text
                FROM ncs_query_aliases
                WHERE unit_code IS NOT NULL
                GROUP BY unit_code
            )
            SELECT cu.unit_code, cu.unit_name_raw, cu.api_definition, cu.unit_level_raw,
                   c.major_code, c.major_name, c.middle_code, c.middle_name,
                   c.small_code, c.small_name, c.sub_code, c.sub_name, c.duty_order,
                   aliases.alias_search_text,
                   ({tier}) AS computed_tier,
                   ({matched_count}) AS matched_token_count,
                   CASE WHEN cu.unit_code = :exact
                          OR TRIM(cu.unit_name_raw) = TRIM(:exact) COLLATE NOCASE
                        THEN 1 ELSE 0 END AS exact_boost,
                   CASE WHEN cu.unit_name_raw LIKE :prefix_pattern ESCAPE '\\'
                        THEN 1 ELSE 0 END AS prefix_boost,
                   CASE
                       WHEN cu.unit_name_raw LIKE :phrase_pattern ESCAPE '\\' THEN 0
                       WHEN c.major_name LIKE :phrase_pattern ESCAPE '\\'
                         OR c.middle_name LIKE :phrase_pattern ESCAPE '\\'
                         OR c.small_name LIKE :phrase_pattern ESCAPE '\\'
                         OR c.sub_name LIKE :phrase_pattern ESCAPE '\\' THEN 1
                       WHEN cu.api_definition LIKE :phrase_pattern ESCAPE '\\' THEN 2
                       ELSE 3
                   END AS field_rank
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            LEFT JOIN alias_search aliases ON aliases.unit_code = cu.unit_code
            WHERE {any_match}
            ORDER BY computed_tier, exact_boost DESC, matched_token_count DESC,
                     prefix_boost DESC, field_rank, LENGTH(cu.unit_name_raw), cu.unit_code
            LIMIT :candidate_limit
        """
    elif item_type == "element":
        columns = ("ce.element_name_raw",)
        any_match, matched_count, phrase_match, tier = _ranking_expressions(columns, tokens)
        sql = f"""
            SELECT ce.element_id, ce.element_name_raw, ce.unit_code, cu.unit_name_raw,
                   ({tier}) AS computed_tier,
                   ({matched_count}) AS matched_token_count,
                   CASE WHEN TRIM(ce.element_name_raw) = TRIM(:exact) COLLATE NOCASE
                        THEN 1 ELSE 0 END AS exact_boost,
                   CASE WHEN ce.element_name_raw LIKE :prefix_pattern ESCAPE '\\'
                        THEN 1 ELSE 0 END AS prefix_boost
            FROM competency_elements ce
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE {any_match}
            ORDER BY computed_tier, exact_boost DESC, matched_token_count DESC,
                     prefix_boost DESC, LENGTH(ce.element_name_raw), ce.element_id
            LIMIT :candidate_limit
        """
    elif item_type == "criteria":
        columns = ("pc.criteria_text_raw", "pc.criteria_text_refined")
        any_match, matched_count, phrase_match, tier = _ranking_expressions(columns, tokens)
        sql = f"""
            SELECT pc.criteria_id, pc.criteria_text_raw, pc.criteria_text_refined,
                   ce.element_id, ce.element_name_raw, ce.unit_code, cu.unit_name_raw,
                   ({tier}) AS computed_tier,
                   ({matched_count}) AS matched_token_count,
                   CASE WHEN TRIM(pc.criteria_text_raw) = TRIM(:exact) COLLATE NOCASE
                        THEN 1 ELSE 0 END AS exact_boost,
                   CASE WHEN pc.criteria_text_raw LIKE :prefix_pattern ESCAPE '\\'
                        THEN 1 ELSE 0 END AS prefix_boost
            FROM performance_criteria pc
            JOIN competency_elements ce ON ce.element_id = pc.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE {any_match}
            ORDER BY computed_tier, exact_boost DESC, matched_token_count DESC,
                     prefix_boost DESC, pc.criteria_id
            LIMIT :candidate_limit
        """
    elif item_type == "ksa":
        columns = ("ki.ksa_text_raw", "ki.ksa_text_refined")
        any_match, matched_count, phrase_match, tier = _ranking_expressions(columns, tokens)
        sql = f"""
            SELECT ki.ksa_id, ki.ksa_type_name, ki.ksa_text_raw, ki.ksa_text_refined,
                   ce.element_id, ce.element_name_raw, ce.unit_code, cu.unit_name_raw,
                   ({tier}) AS computed_tier,
                   ({matched_count}) AS matched_token_count,
                   CASE WHEN TRIM(ki.ksa_text_raw) = TRIM(:exact) COLLATE NOCASE
                        THEN 1 ELSE 0 END AS exact_boost,
                   CASE WHEN ki.ksa_text_raw LIKE :prefix_pattern ESCAPE '\\'
                        THEN 1 ELSE 0 END AS prefix_boost
            FROM ksa_items ki
            JOIN competency_elements ce ON ce.element_id = ki.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE {any_match}
            ORDER BY computed_tier, exact_boost DESC, matched_token_count DESC,
                     prefix_boost DESC, ki.ksa_id
            LIMIT :candidate_limit
        """
    else:
        raise ValueError(f"unsupported search type: {item_type}")

    params: dict[str, Any] = {
        "exact": phrase,
        "phrase_pattern": f"%{escape_like(phrase)}%",
        "prefix_pattern": f"{escape_like(phrase)}%",
        "candidate_limit": candidate_limit,
    }
    params.update(
        {
            f"token_{index}": f"%{escape_like(token)}%"
            for index, token in enumerate(tokens)
        }
    )
    return conn.execute(sql, params).fetchall()


def _row_to_result(item_type: str, row: sqlite3.Row) -> dict[str, Any]:
    if item_type == "unit":
        result = {
            "type": "unit",
            "id": row["unit_code"],
            "text": row["unit_name_raw"],
            "unit_level": row["unit_level_raw"],
            "path": {
                "major_code": row["major_code"],
                "major_name": row["major_name"],
                "middle_code": row["middle_code"],
                "middle_name": row["middle_name"],
                "small_code": row["small_code"],
                "small_name": row["small_name"],
                "sub_code": row["sub_code"],
                "sub_name": row["sub_name"],
            },
            "api_definition": row["api_definition"],
            "_search_fields": {
                "unit_code": row["unit_code"],
                "unit_name": row["unit_name_raw"],
                "definition": row["api_definition"],
                "classification": " ".join(
                    str(row[key] or "")
                    for key in ("major_name", "middle_name", "small_name", "sub_name")
                ),
                "alias": row["alias_search_text"],
            },
        }
    elif item_type == "element":
        result = {
            "type": "element",
            "id": row["element_id"],
            "text": row["element_name_raw"],
            "path": {
                "unit_code": row["unit_code"],
                "unit_name": row["unit_name_raw"],
            },
            "_search_fields": {"element_name": row["element_name_raw"]},
        }
    elif item_type == "criteria":
        result = {
            "type": "criteria",
            "id": row["criteria_id"],
            "text": row["criteria_text_raw"],
            "path": {
                "unit_code": row["unit_code"],
                "unit_name": row["unit_name_raw"],
                "element_id": row["element_id"],
                "element_name": row["element_name_raw"],
            },
            "_search_fields": {
                "criteria_text": row["criteria_text_raw"],
                "criteria_text_refined": row["criteria_text_refined"],
            },
        }
    else:
        result = {
            "type": "ksa",
            "id": row["ksa_id"],
            "text": row["ksa_text_raw"],
            "ksa_type": row["ksa_type_name"],
            "path": {
                "unit_code": row["unit_code"],
                "unit_name": row["unit_name_raw"],
                "element_id": row["element_id"],
                "element_name": row["element_name_raw"],
            },
            "_search_fields": {
                "ksa_text": row["ksa_text_raw"],
                "ksa_text_refined": row["ksa_text_refined"],
            },
        }
    result["_computed_tier"] = int(row["computed_tier"])
    result["matched_token_count"] = int(row["matched_token_count"])
    return result


def _annotate_result(result: dict[str, Any], phrase: str, tokens: list[str]) -> None:
    fields = [normalize_text(value).casefold() for value in result.pop("_search_fields").values()]
    matched_tokens = [
        token for token in tokens if any(token.casefold() in field for field in fields)
    ]
    tier = int(result.pop("_computed_tier"))
    result["match_mode"] = MATCH_MODES[tier]
    result["matched_tokens"] = matched_tokens
    result["phrase_match"] = bool(
        phrase and any(phrase.casefold() in field for field in fields)
    )


def _round_robin(
    candidates_by_type: dict[str, list[dict[str, Any]]],
    requested_types: tuple[str, ...],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index = 0
    while True:
        appended = False
        for item_type in requested_types:
            candidates = candidates_by_type[item_type]
            if index < len(candidates):
                merged.append(candidates[index])
                appended = True
        if not appended:
            return merged
        index += 1


def candidate_search(
    conn: sqlite3.Connection,
    query: str,
    scope: str = "all",
    limit: int = 20,
    offset: int = 0,
    *,
    normalize_fn: Callable[[str], tuple[str, list[str]]] = normalize_query,
) -> dict[str, Any]:
    max_rows = min(max(int(limit), 1), 100)
    applied_offset = min(max(int(offset), 0), 10_000)
    normalized_scope = scope if scope in SEARCH_TYPES or scope == "all" else "all"
    requested_types = SEARCH_TYPES if normalized_scope == "all" else (normalized_scope,)
    phrase, tokens = normalize_fn(query)
    tokens = list(tokens[:4])
    empty_counts = {item_type: 0 for item_type in requested_types}
    if not phrase or not tokens:
        return {
            "query": query,
            "normalized_query": phrase,
            "query_tokens": tokens,
            "scope": normalized_scope,
            "counts_by_type": empty_counts,
            "returned": 0,
            "offset": applied_offset,
            "next_offset": None,
            "results": [],
            "_sql_statement_count": 0,
        }

    candidate_limit = applied_offset + max_rows + 1
    candidates_by_type: dict[str, list[dict[str, Any]]] = {}
    match_mode_by_type: dict[str, str | None] = {}
    for item_type in requested_types:
        rows = _query_rows(
            conn,
            item_type,
            phrase=phrase,
            tokens=tokens,
            candidate_limit=candidate_limit,
        )
        mapped = [_row_to_result(item_type, row) for row in rows]
        selected_tier = min(
            (int(item["_computed_tier"]) for item in mapped),
            default=None,
        )
        selected = [
            item for item in mapped if item["_computed_tier"] == selected_tier
        ]
        candidates_by_type[item_type] = selected
        match_mode_by_type[item_type] = (
            MATCH_MODES[selected_tier] if selected_tier is not None else None
        )

    merged = _round_robin(candidates_by_type, requested_types)
    page_end = applied_offset + max_rows
    page = merged[applied_offset:page_end]
    consumed = Counter(item["type"] for item in merged[:page_end])
    has_more_by_type = {
        item_type: len(candidates_by_type[item_type]) > consumed[item_type]
        or len(candidates_by_type[item_type]) == candidate_limit
        for item_type in requested_types
    }
    counts = Counter(item["type"] for item in page)
    for item in page:
        _annotate_result(item, phrase, tokens)
    next_offset = page_end if page and any(has_more_by_type.values()) else None
    active_modes = {mode for mode in match_mode_by_type.values() if mode}
    match_mode = (
        next(iter(active_modes))
        if len(active_modes) == 1
        else "mixed" if active_modes else None
    )
    return {
        "query": query,
        "normalized_query": phrase,
        "query_tokens": tokens,
        "scope": normalized_scope,
        "match_mode": match_mode,
        "match_mode_by_type": match_mode_by_type,
        "counts_by_type": {item_type: counts[item_type] for item_type in requested_types},
        "has_more_by_type": has_more_by_type,
        "returned": len(page),
        "offset": applied_offset,
        "next_offset": next_offset,
        "results": page,
        "_sql_statement_count": len(requested_types),
    }


def _payload_proxies(
    payload: dict[str, Any],
    preferred_types: list[str],
) -> dict[str, float]:
    results = payload.get("results", [])[:10]
    tokens = payload.get("query_tokens", [])
    if not results:
        return {
            "lexical_token_coverage": 0.0,
            "off_scope_proxy": 0.0,
            "common_token_only_proxy": 0.0,
            "type_imbalance_proxy": 0.0,
            "duplicate_proxy": 0.0,
            "aggregate_risk_proxy": 0.0,
        }
    lexical = sum(
        min(len(item.get("matched_tokens", [])) / max(len(tokens), 1), 1.0)
        for item in results
    ) / len(results)
    allowed = set(preferred_types)
    off_scope = sum(item.get("type") not in allowed for item in results) / len(results)
    common_only = (
        sum(len(item.get("matched_tokens", [])) <= 1 for item in results) / len(results)
        if len(tokens) > 1
        else 0.0
    )
    counts = Counter(str(item.get("type")) for item in results)
    expected = [item_type for item_type in SEARCH_TYPES if item_type in allowed]
    type_values = [counts[item_type] for item_type in expected]
    type_imbalance = (
        (max(type_values) - min(type_values)) / max(max(type_values), 1)
        if len(type_values) > 1
        else 0.0
    )
    ids = stable_ids(payload)
    duplicate = 1.0 - (len(set(ids)) / len(ids))
    aggregate = (off_scope + common_only + type_imbalance + duplicate) / 4.0
    return {
        "lexical_token_coverage": round(lexical, 6),
        "off_scope_proxy": round(off_scope, 6),
        "common_token_only_proxy": round(common_only, 6),
        "type_imbalance_proxy": round(type_imbalance, 6),
        "duplicate_proxy": round(duplicate, 6),
        "aggregate_risk_proxy": round(aggregate, 6),
    }


def _overlap_at_10(baseline: dict[str, Any], candidate: dict[str, Any]) -> float:
    baseline_ids = set(stable_ids(baseline))
    candidate_ids = set(stable_ids(candidate))
    if not baseline_ids and not candidate_ids:
        return 1.0
    denominator = max(len(baseline_ids), len(candidate_ids), 1)
    return round(len(baseline_ids & candidate_ids) / denominator, 6)


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0


def _baseline_runner(server: Any) -> Callable[[str, str, int, int], tuple[dict[str, Any], int]]:
    def run(query: str, scope: str, limit: int, offset: int) -> tuple[dict[str, Any], int]:
        statements: list[str] = []
        original_open_db = server.open_db

        @contextmanager
        def traced_open_db() -> Any:
            with original_open_db() as conn:
                conn.set_trace_callback(
                    lambda sql: statements.append(sql)
                    if sql.lstrip().upper().startswith(("SELECT", "WITH"))
                    else None
                )
                try:
                    yield conn
                finally:
                    conn.set_trace_callback(None)

        server.open_db = traced_open_db
        try:
            payload = server.search_ncs(query, scope=scope, limit=limit, offset=offset)
        finally:
            server.open_db = original_open_db
        return payload, len(statements)

    return run


def _candidate_runner(server: Any) -> Callable[[str, str, int, int], tuple[dict[str, Any], int]]:
    def runtime_normalize(query: str) -> tuple[str, list[str]]:
        phrase, query_tokens, fallback_tokens = server._normalize_ncs_search_query(query)
        return phrase, list(fallback_tokens or query_tokens)

    def run(query: str, scope: str, limit: int, offset: int) -> tuple[dict[str, Any], int]:
        with server.open_db() as conn:
            payload = candidate_search(
                conn,
                query,
                scope=scope,
                limit=limit,
                offset=offset,
                normalize_fn=runtime_normalize,
            )
        return payload, int(payload.pop("_sql_statement_count"))

    return run


def benchmark_candidates(
    candidates: list[dict[str, Any]],
    baseline_run: Callable[[str, str, int, int], tuple[dict[str, Any], int]],
    candidate_run: Callable[[str, str, int, int], tuple[dict[str, Any], int]],
    *,
    runs: int,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    all_latency = {"baseline": [], "candidate": []}
    all_statements = {"baseline": [], "candidate": []}
    rss_samples: dict[str, list[int]] = {"baseline": [], "candidate": []}
    for case in candidates:
        query = str(case["query"])
        scope = str(case.get("scope_candidate") or "all")
        preferred = list(case.get("preferred_result_type_candidates") or SEARCH_TYPES)
        baseline_run(query, scope, limit, 0)
        candidate_run(query, scope, limit, 0)
        payloads: dict[str, list[dict[str, Any]]] = {"baseline": [], "candidate": []}
        latencies: dict[str, list[float]] = {"baseline": [], "candidate": []}
        statements: dict[str, list[int]] = {"baseline": [], "candidate": []}
        for run_index in range(runs):
            order = ("baseline", "candidate") if run_index % 2 == 0 else ("candidate", "baseline")
            for strategy in order:
                runner = baseline_run if strategy == "baseline" else candidate_run
                rss_before = current_rss_bytes()
                started = time.perf_counter()
                payload, statement_count = runner(query, scope, limit, 0)
                elapsed = (time.perf_counter() - started) * 1000.0
                rss_after = current_rss_bytes()
                payloads[strategy].append(payload)
                latencies[strategy].append(elapsed)
                statements[strategy].append(statement_count)
                all_latency[strategy].append(elapsed)
                all_statements[strategy].append(statement_count)
                if rss_before is not None:
                    rss_samples[strategy].append(rss_before)
                if rss_after is not None:
                    rss_samples[strategy].append(rss_after)

        baseline_payload = payloads["baseline"][-1]
        candidate_payload = payloads["candidate"][-1]
        baseline_ids = [stable_ids(payload) for payload in payloads["baseline"]]
        candidate_ids = [stable_ids(payload) for payload in payloads["candidate"]]
        records.append(
            {
                "case_id": case["case_id"],
                "query": query,
                "scope": scope,
                "tags": case.get("tags", []),
                "preferred_result_type_candidates": preferred,
                "human_label_present": bool(case.get("gold_label_present")),
                "baseline": {
                    "latency": latency_summary(latencies["baseline"]),
                    "sql_statement_counts": statements["baseline"],
                    "result_count": baseline_payload.get("returned", 0),
                    "zero_hit": not bool(baseline_payload.get("results")),
                    "top10_ids": stable_ids(baseline_payload),
                    "deterministic": all(ids == baseline_ids[0] for ids in baseline_ids),
                    "proxies": _payload_proxies(baseline_payload, preferred),
                },
                "candidate": {
                    "latency": latency_summary(latencies["candidate"]),
                    "sql_statement_counts": statements["candidate"],
                    "result_count": candidate_payload.get("returned", 0),
                    "zero_hit": not bool(candidate_payload.get("results")),
                    "top10_ids": stable_ids(candidate_payload),
                    "deterministic": all(ids == candidate_ids[0] for ids in candidate_ids),
                    "proxies": _payload_proxies(candidate_payload, preferred),
                },
                "top10_overlap": _overlap_at_10(baseline_payload, candidate_payload),
            }
        )

    rss_summary: dict[str, Any] = {}
    for strategy in ("baseline", "candidate"):
        samples = rss_samples[strategy]
        rss_summary[strategy] = {
            "supported": bool(samples),
            "sampling": "process RSS immediately before and after each invocation",
            "min_bytes": min(samples) if samples else None,
            "max_bytes": max(samples) if samples else None,
        }
    aggregate = {
        "latency": {
            "baseline": latency_summary(all_latency["baseline"]),
            "candidate": latency_summary(all_latency["candidate"]),
        },
        "sql_statement_count": {
            "baseline_mean": _mean([float(value) for value in all_statements["baseline"]]),
            "candidate_mean": _mean([float(value) for value in all_statements["candidate"]]),
            "baseline_total": sum(all_statements["baseline"]),
            "candidate_total": sum(all_statements["candidate"]),
        },
        "rss": rss_summary,
    }
    return records, aggregate


def _gate(report_records: list[dict[str, Any]], aggregate: dict[str, Any]) -> dict[str, Any]:
    baseline_latency = aggregate["latency"]["baseline"]
    candidate_latency = aggregate["latency"]["candidate"]
    baseline_p50 = float(baseline_latency["p50_ms"] or 0.0)
    candidate_p50 = float(candidate_latency["p50_ms"] or 0.0)
    baseline_p95 = float(baseline_latency["p95_ms"] or 0.0)
    candidate_p95 = float(candidate_latency["p95_ms"] or 0.0)
    p50_improvement = (baseline_p50 - candidate_p50) / baseline_p50 if baseline_p50 else 0.0
    p95_improvement = (baseline_p95 - candidate_p95) / baseline_p95 if baseline_p95 else 0.0
    overlaps = [float(record["top10_overlap"]) for record in report_records]
    baseline_risk = _mean(
        [float(record["baseline"]["proxies"]["aggregate_risk_proxy"]) for record in report_records]
    )
    candidate_risk = _mean(
        [float(record["candidate"]["proxies"]["aggregate_risk_proxy"]) for record in report_records]
    )
    baseline_rss = aggregate["rss"]["baseline"]["max_bytes"]
    candidate_rss = aggregate["rss"]["candidate"]["max_bytes"]
    rss_delta = (
        int(candidate_rss) - int(baseline_rss)
        if baseline_rss is not None and candidate_rss is not None
        else None
    )
    baseline_zero = sum(bool(record["baseline"]["zero_hit"]) for record in report_records)
    candidate_zero = sum(bool(record["candidate"]["zero_hit"]) for record in report_records)
    checks = {
        "p50_improvement_at_least_25_percent": p50_improvement >= 0.25,
        "p95_local_at_most_1000ms_or_25_percent_better": candidate_p95 <= 1000.0
        or p95_improvement >= 0.25,
        "top10_overlap_mean_at_least_0_99": _mean(overlaps) >= 0.99,
        "top10_overlap_min_at_least_0_80": min(overlaps) >= 0.80 if overlaps else False,
        "risk_proxy_delta_at_most_0_05": candidate_risk - baseline_risk <= 0.05,
        "rss_delta_at_most_50mb": rss_delta is not None and rss_delta <= 50 * 1024 * 1024,
        "zero_hit_not_worse": candidate_zero <= baseline_zero,
        "result_determinism": all(
            record["baseline"]["deterministic"] and record["candidate"]["deterministic"]
            for record in report_records
        ),
    }
    performance_gate_pass = all(checks.values())
    return {
        "checks": checks,
        "performance_gate_pass": performance_gate_pass,
        "p50_improvement_ratio": round(p50_improvement, 6),
        "p95_improvement_ratio": round(p95_improvement, 6),
        "top10_overlap_mean": _mean(overlaps),
        "top10_overlap_min": min(overlaps) if overlaps else None,
        "baseline_risk_proxy_mean": baseline_risk,
        "candidate_risk_proxy_mean": candidate_risk,
        "risk_proxy_delta": round(candidate_risk - baseline_risk, 6),
        "baseline_zero_hit_count": baseline_zero,
        "candidate_zero_hit_count": candidate_zero,
        "rss_delta_bytes": rss_delta,
        "automatic_product_promotion": False,
        "promotion_verdict": (
            "hold_for_human_relevance_review"
            if performance_gate_pass
            else "do_not_promote_gate_failed"
        ),
        "reason": (
            "The 50-query pack has no human relevance labels; lexical/risk proxies cannot prove "
            "Recall@K, MRR, nDCG, or off-scope correctness."
        ),
    }


def build_report(
    *,
    db_path: Path,
    eval_path: Path,
    runs: int,
    limit: int,
) -> dict[str, Any]:
    evaluation = json.loads(eval_path.read_text(encoding="utf-8"))
    candidates = list(evaluation.get("candidates", []))
    if len(candidates) != 50:
        raise ValueError(f"expected 50 candidate_eval rows, found {len(candidates)}")
    os.environ["NCS_DB_PATH"] = str(db_path.resolve())
    os.environ["NCS_MCP_READ_ONLY_MODE"] = "true"
    os.environ["NCS_MCP_OPERATOR_TOOLS"] = "false"
    from ncs_mcp import server

    records, aggregate = benchmark_candidates(
        candidates,
        _baseline_runner(server),
        _candidate_runner(server),
        runs=runs,
        limit=limit,
    )
    gate = _gate(records, aggregate)
    return {
        "schema": SCHEMA,
        "version": 1,
        "generated_at": generated_at(),
        "mode": "read_only_single_pass_experiment",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "database_path": str(db_path),
            "latency_caveat": "Local warm-biased measurements; not Vercel cold-start latency.",
        },
        "evaluation_contract": {
            "source": str(eval_path),
            "candidate_count": len(candidates),
            "status": "candidate_eval",
            "gold_labels_present": False,
            "human_review_required": True,
            "human_decision_present": False,
            "metrics_not_claimed": ["Recall@5", "Recall@10", "MRR@10", "nDCG@10", "off_scope_rate"],
            "proxies_only": [
                "lexical_token_coverage",
                "off_scope_proxy",
                "common_token_only_proxy",
                "type_imbalance_proxy",
                "duplicate_proxy",
            ],
        },
        "strategies": {
            "baseline": "current ncs_mcp.server.search_ncs lazy phrase -> token-AND -> token-OR tiers",
            "candidate": (
                "one SQL statement per requested type; token OR scan with computed phrase/token-AND tier, "
                "matched-token count, exact/prefix boost, and deterministic ID tie-break"
            ),
        },
        "benchmark": {
            "runs_per_case": runs,
            "limit": limit,
            "query_count": len(candidates),
            "aggregate": aggregate,
            "records": records,
        },
        "gate": gate,
        "safety": {
            "database_open_mode": "read_only",
            "database_writes": False,
            "product_code_changes": False,
            "status_updates": False,
            "human_review_claim": False,
        },
        "reproduce": (
            "python scripts/benchmark_ncs_single_pass_search.py "
            f"--db \"{db_path}\" --eval \"{eval_path}\" --runs {runs} --limit {limit}"
        ),
    }


def render_markdown(report: dict[str, Any]) -> str:
    aggregate = report["benchmark"]["aggregate"]
    gate = report["gate"]
    baseline_latency = aggregate["latency"]["baseline"]
    candidate_latency = aggregate["latency"]["candidate"]
    lines = [
        "# NCS Single-Pass Search Experiment",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Candidate queries: `{report['benchmark']['query_count']}`",
        f"- Runs per query: `{report['benchmark']['runs_per_case']}`",
        f"- Performance gate: `{gate['performance_gate_pass']}`",
        f"- Promotion verdict: `{gate['promotion_verdict']}`",
        "- Human relevance labels: `absent`",
        "- Product code or database writes: `false`",
        "",
        "## Aggregate",
        "",
        "| Metric | Lazy-tier baseline | Single-pass candidate |",
        "| --- | ---: | ---: |",
        f"| p50 ms | {baseline_latency['p50_ms']} | {candidate_latency['p50_ms']} |",
        f"| p95 ms | {baseline_latency['p95_ms']} | {candidate_latency['p95_ms']} |",
        f"| max ms | {baseline_latency['max_ms']} | {candidate_latency['max_ms']} |",
        f"| mean SQL statements | {aggregate['sql_statement_count']['baseline_mean']} | {aggregate['sql_statement_count']['candidate_mean']} |",
        f"| zero-hit count | {gate['baseline_zero_hit_count']} | {gate['candidate_zero_hit_count']} |",
        f"| risk proxy mean | {gate['baseline_risk_proxy_mean']} | {gate['candidate_risk_proxy_mean']} |",
        "",
        f"- p50 improvement ratio: `{gate['p50_improvement_ratio']}`",
        f"- p95 improvement ratio: `{gate['p95_improvement_ratio']}`",
        f"- Top-10 overlap mean/min: `{gate['top10_overlap_mean']}` / `{gate['top10_overlap_min']}`",
        f"- RSS delta bytes: `{gate['rss_delta_bytes']}`",
        "",
        "## Gate Checks",
        "",
    ]
    for name, passed in gate["checks"].items():
        lines.append(f"- `{name}`: `{passed}`")
    lines.extend(
        [
            "",
            "## Per-Query Evidence",
            "",
            "| ID | Query | Scope | Base p50 | Candidate p50 | Base SQL | Candidate SQL | Overlap@10 | Base risk | Candidate risk |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for record in report["benchmark"]["records"]:
        baseline = record["baseline"]
        candidate = record["candidate"]
        lines.append(
            "| "
            + " | ".join(
                [
                    str(record["case_id"]),
                    str(record["query"]).replace("|", "\\|"),
                    str(record["scope"]),
                    str(baseline["latency"]["p50_ms"]),
                    str(candidate["latency"]["p50_ms"]),
                    str(round(sum(baseline["sql_statement_counts"]) / len(baseline["sql_statement_counts"]), 3)),
                    str(round(sum(candidate["sql_statement_counts"]) / len(candidate["sql_statement_counts"]), 3)),
                    str(record["top10_overlap"]),
                    str(baseline["proxies"]["aggregate_risk_proxy"]),
                    str(candidate["proxies"]["aggregate_risk_proxy"]),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            f"- `{gate['reason']}`",
            "- The risk fields are lexical/type/duplicate proxies, not judged off-scope labels.",
            "- RSS is sampled at invocation boundaries and may miss a transient intra-query peak.",
            "- A failed gate means the candidate must not be promoted.",
            "- A passed performance gate still requires human relevance review before product promotion.",
            "",
            "## Reproduce",
            "",
            "```powershell",
            report["reproduce"],
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare lazy-tier and single-pass NCS search.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--eval", type=Path, default=DEFAULT_EVAL)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    if args.runs < 1 or args.runs > 20:
        parser.error("--runs must be between 1 and 20")
    if args.limit < 1 or args.limit > 100:
        parser.error("--limit must be between 1 and 100")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(
        db_path=args.db.resolve(),
        eval_path=args.eval.resolve(),
        runs=args.runs,
        limit=args.limit,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report["gate"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
