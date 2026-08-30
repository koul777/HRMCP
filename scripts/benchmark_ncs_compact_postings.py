from __future__ import annotations

import argparse
from array import array
from contextlib import closing, contextmanager
import ctypes
import gc
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.compact_postings import decode_posting_ids, encode_posting_ids


SCHEMA = "ncs_compact_search_postings_experiment_v1"
TYPE_CODES = {"unit": 1, "element": 2, "criteria": 3, "ksa": 4}
TYPE_ORDER = tuple(TYPE_CODES)
DEFAULT_ARCHIVE = ROOT / "deploy" / "vercel_mcp_app" / "api" / "ncs_ontology_compact.zip"
DEFAULT_MANIFEST = ROOT / "deploy" / "vercel_mcp_app" / "api" / "ncs_ontology_compact.manifest.json"
DEFAULT_CANDIDATES = ROOT / "reports" / "ncs_search_eval_candidates_20260830.json"
DEFAULT_OUT = ROOT / "reports" / "ncs_compact_postings_experiment_20260830.json"
DEFAULT_MARKDOWN_OUT = ROOT / "reports" / "ncs_compact_postings_experiment_20260830.md"
HARD_SNAPSHOT_CAP_BYTES = 480_000_000
SOFT_SNAPSHOT_CAP_BYTES = 460_000_000
INDEX_TARGET_BYTES = 30_000_000
LAZY_TIER_P50_MS = 324.0


def generated_at() -> str:
    return datetime.now(UTC).isoformat()


def percentile(values: Iterable[float], value: float) -> float | None:
    ordered = sorted(float(item) for item in values)
    if not ordered:
        return None
    rank = max(1, math.ceil((value / 100.0) * len(ordered)))
    return round(ordered[rank - 1], 3)


def latency_summary(values: list[float]) -> dict[str, Any]:
    rounded = [round(value, 3) for value in values]
    return {
        "sample_count": len(rounded),
        "samples": rounded,
        "min": round(min(rounded), 3) if rounded else None,
        "p50": percentile(rounded, 50),
        "p95": percentile(rounded, 95),
        "max": round(max(rounded), 3) if rounded else None,
    }


def normalize_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    folded = "".join(
        " " if unicodedata.category(character)[:1] in {"P", "S"} else character
        for character in normalized
    )
    return " ".join(folded.split())


def index_terms(value: Any) -> set[str]:
    return set(normalize_text(value).split())


def query_tokens(value: Any) -> tuple[str, list[str]]:
    phrase = normalize_text(value)
    return phrase, phrase.split()[:4]


def _process_memory_bytes() -> dict[str, int | None]:
    if os.name == "nt":
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
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        get_process_memory_info = psapi.GetProcessMemoryInfo
        get_process_memory_info.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        get_process_memory_info.restype = wintypes.BOOL
        handle = kernel32.GetCurrentProcess()
        ok = get_process_memory_info(
            handle, ctypes.byref(counters), counters.cb
        )
        if ok:
            return {
                "rss": int(counters.WorkingSetSize),
                "peak_rss": int(counters.PeakWorkingSetSize),
            }
    try:
        import resource

        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if sys.platform != "darwin":
            peak *= 1024
        return {"rss": None, "peak_rss": peak}
    except (ImportError, OSError, ValueError):
        return {"rss": None, "peak_rss": None}


@contextmanager
def _readonly_connection(path: Path) -> Iterator[sqlite3.Connection]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _limited(sql: str, sample_rows_per_type: int | None) -> tuple[str, tuple[int, ...]]:
    if sample_rows_per_type is None:
        return sql, ()
    return f"{sql}\nLIMIT ?", (sample_rows_per_type,)


def iter_source_documents(
    conn: sqlite3.Connection,
    item_type: str,
    *,
    sample_rows_per_type: int | None = None,
) -> Iterator[dict[str, Any]]:
    if item_type == "unit":
        sql, params = _limited(
            """
            WITH alias_search AS (
                SELECT unit_code,
                       GROUP_CONCAT(COALESCE(alias_text, '') || ' ' ||
                                    COALESCE(normalized_query, ''), ' ') AS aliases
                FROM ncs_query_aliases
                WHERE unit_code IS NOT NULL
                GROUP BY unit_code
            )
            SELECT cu.unit_code, cu.unit_name_raw, cu.api_definition,
                   c.major_name, c.middle_name, c.small_name, c.sub_name,
                   aliases.aliases
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            LEFT JOIN alias_search aliases ON aliases.unit_code = cu.unit_code
            ORDER BY cu.unit_code
            """,
            sample_rows_per_type,
        )
        for doc_id, row in enumerate(conn.execute(sql, params), start=1):
            yield {
                "doc_id": doc_id,
                "source_id": str(row["unit_code"]),
                "text": " ".join(str(value or "") for value in row),
            }
        return

    specifications = {
        "element": (
            "element_id",
            "SELECT element_id, element_name_raw FROM competency_elements ORDER BY element_id",
        ),
        "criteria": (
            "criteria_id",
            """SELECT criteria_id, criteria_text_raw, criteria_text_refined
               FROM performance_criteria ORDER BY criteria_id""",
        ),
        "ksa": (
            "ksa_id",
            """SELECT ksa_id, ksa_text_raw, ksa_text_refined
               FROM ksa_items ORDER BY ksa_id""",
        ),
    }
    id_column, base_sql = specifications[item_type]
    sql, params = _limited(base_sql, sample_rows_per_type)
    for row in conn.execute(sql, params):
        yield {
            "doc_id": int(row[id_column]),
            "source_id": int(row[id_column]),
            "text": " ".join(str(value or "") for value in row[1:]),
        }


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = {
        "unit": "competency_units",
        "element": "competency_elements",
        "criteria": "performance_criteria",
        "ksa": "ksa_items",
    }
    return {
        item_type: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for item_type, table in tables.items()
    }


def build_index(
    source_path: Path,
    index_path: Path,
    *,
    sample_rows_per_type: int | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    memory_before = _process_memory_bytes()
    type_stats: dict[str, dict[str, Any]] = {}
    total_posting_ids = 0
    total_encoded_bytes = 0
    total_tokens = 0

    with _readonly_connection(source_path) as source, closing(
        sqlite3.connect(index_path)
    ) as target:
        source_counts = _table_counts(source)
        target.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=MEMORY;
            PRAGMA cache_size=-65536;
            CREATE TABLE token_postings (
                type_code INTEGER NOT NULL,
                token TEXT NOT NULL,
                doc_count INTEGER NOT NULL,
                doc_ids BLOB NOT NULL,
                PRIMARY KEY(type_code, token)
            ) WITHOUT ROWID;
            CREATE TABLE unit_doc_keys (
                doc_id INTEGER PRIMARY KEY,
                unit_code TEXT NOT NULL UNIQUE
            );
            """
        )
        for item_type in TYPE_ORDER:
            type_started = time.perf_counter()
            postings: dict[str, array[int]] = {}
            indexed_rows = 0
            unit_keys: list[tuple[int, str]] = []
            for document in iter_source_documents(
                source,
                item_type,
                sample_rows_per_type=sample_rows_per_type,
            ):
                indexed_rows += 1
                doc_id = int(document["doc_id"])
                if item_type == "unit":
                    unit_keys.append((doc_id, str(document["source_id"])))
                for term in index_terms(document["text"]):
                    postings.setdefault(term, array("Q")).append(doc_id)
            if unit_keys:
                target.executemany(
                    "INSERT INTO unit_doc_keys(doc_id, unit_code) VALUES (?, ?)",
                    unit_keys,
                )

            type_posting_ids = 0
            type_encoded_bytes = 0
            insert_batch: list[tuple[int, str, int, bytes]] = []
            for term in sorted(postings):
                ids = postings[term]
                payload = encode_posting_ids(ids)
                doc_count = len(ids)
                type_posting_ids += doc_count
                type_encoded_bytes += len(payload)
                insert_batch.append((TYPE_CODES[item_type], term, doc_count, payload))
                if len(insert_batch) >= 2000:
                    target.executemany(
                        "INSERT INTO token_postings VALUES (?, ?, ?, ?)", insert_batch
                    )
                    insert_batch.clear()
            if insert_batch:
                target.executemany(
                    "INSERT INTO token_postings VALUES (?, ?, ?, ?)", insert_batch
                )
            target.commit()
            token_count = len(postings)
            total_posting_ids += type_posting_ids
            total_encoded_bytes += type_encoded_bytes
            total_tokens += token_count
            type_stats[item_type] = {
                "source_rows": source_counts[item_type],
                "indexed_rows": indexed_rows,
                "token_count": token_count,
                "posting_id_count": type_posting_ids,
                "encoded_bytes": type_encoded_bytes,
                "elapsed_ms": round((time.perf_counter() - type_started) * 1000, 3),
                "memory_after": _process_memory_bytes(),
            }
            postings.clear()
            gc.collect()
        target.execute("VACUUM")
        target.commit()

    index_bytes = index_path.stat().st_size
    memory_after = _process_memory_bytes()
    full_build = sample_rows_per_type is None
    indexed_total = sum(item["indexed_rows"] for item in type_stats.values())
    source_total = sum(item["source_rows"] for item in type_stats.values())
    projection: dict[str, Any]
    if full_build:
        projection = {
            "method": "exact_full_build",
            "estimated_full_index_lower_bytes": index_bytes,
            "estimated_full_index_upper_bytes": index_bytes,
        }
    else:
        expansion = source_total / max(indexed_total, 1)
        projection = {
            "method": "sample_linear_range_not_full_build",
            "sample_expansion_factor": round(expansion, 4),
            "estimated_full_index_lower_bytes": int(index_bytes * expansion * 0.8),
            "estimated_full_index_upper_bytes": int(index_bytes * expansion * 1.4),
        }
    return {
        "build_mode": "full_build" if full_build else "not_full_build",
        "sample_rows_per_type": sample_rows_per_type,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "index_bytes": index_bytes,
        "token_count": total_tokens,
        "posting_id_count": total_posting_ids,
        "encoded_bytes": total_encoded_bytes,
        "encoded_bytes_per_posting": round(
            total_encoded_bytes / max(total_posting_ids, 1), 6
        ),
        "sqlite_bytes_per_posting": round(index_bytes / max(total_posting_ids, 1), 6),
        "source_row_count": source_total,
        "indexed_row_count": indexed_total,
        "type_stats": type_stats,
        "memory_before": memory_before,
        "memory_after": memory_after,
        "peak_rss_delta_bytes": (
            max(
                0,
                int(memory_after["peak_rss"] or 0)
                - int(memory_before["peak_rss"] or 0),
            )
            if memory_after["peak_rss"] is not None
            and memory_before["peak_rss"] is not None
            else None
        ),
        "projection": projection,
    }


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _posting_candidates(
    index: sqlite3.Connection,
    item_type: str,
    tokens: list[str],
) -> tuple[set[int], str | None, dict[str, int]]:
    token_sets: list[set[int]] = []
    dictionary_matches: dict[str, int] = {}
    for token in tokens:
        ids: set[int] = set()
        rows = index.execute(
            """SELECT doc_ids FROM token_postings
               WHERE type_code = ? AND token LIKE ? ESCAPE '\\'""",
            (TYPE_CODES[item_type], f"%{_escape_like(token)}%"),
        ).fetchall()
        dictionary_matches[token] = len(rows)
        for row in rows:
            ids.update(decode_posting_ids(row[0]))
        token_sets.append(ids)
    nonempty = [ids for ids in token_sets if ids]
    if not nonempty:
        return set(), None, dictionary_matches
    if len(nonempty) == len(token_sets):
        intersection = set.intersection(*nonempty)
        if intersection:
            return intersection, "token_and", dictionary_matches
    return set.union(*nonempty), "token_or", dictionary_matches


def _chunks(values: Iterable[Any], size: int = 800) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def _load_documents(
    source: sqlite3.Connection,
    index: sqlite3.Connection,
    item_type: str,
    doc_ids: set[int],
) -> list[dict[str, Any]]:
    if not doc_ids:
        return []
    documents: list[dict[str, Any]] = []
    if item_type == "unit":
        key_map: dict[int, str] = {}
        for batch in _chunks(sorted(doc_ids)):
            placeholders = ",".join("?" for _ in batch)
            for row in index.execute(
                f"SELECT doc_id, unit_code FROM unit_doc_keys WHERE doc_id IN ({placeholders})",
                batch,
            ):
                key_map[int(row[0])] = str(row[1])
        reverse = {unit_code: doc_id for doc_id, unit_code in key_map.items()}
        for batch in _chunks(sorted(reverse)):
            placeholders = ",".join("?" for _ in batch)
            rows = source.execute(
                f"""
                WITH alias_search AS (
                    SELECT unit_code,
                           GROUP_CONCAT(COALESCE(alias_text, '') || ' ' ||
                                        COALESCE(normalized_query, ''), ' ') AS aliases
                    FROM ncs_query_aliases WHERE unit_code IS NOT NULL GROUP BY unit_code
                )
                SELECT cu.unit_code, cu.unit_name_raw, cu.api_definition,
                       c.major_name, c.middle_name, c.small_name, c.sub_name,
                       aliases.aliases
                FROM competency_units cu
                JOIN classifications c ON c.classification_id = cu.classification_id
                LEFT JOIN alias_search aliases ON aliases.unit_code = cu.unit_code
                WHERE cu.unit_code IN ({placeholders})
                """,
                batch,
            )
            for row in rows:
                classification = " ".join(
                    str(row[key] or "")
                    for key in ("major_name", "middle_name", "small_name", "sub_name")
                )
                combined = " ".join(str(value or "") for value in row)
                documents.append(
                    {
                        "doc_id": reverse[str(row["unit_code"])],
                        "source_id": str(row["unit_code"]),
                        "text": str(row["unit_name_raw"] or ""),
                        "combined": normalize_text(combined),
                        "name": normalize_text(row["unit_name_raw"]),
                        "classification": normalize_text(classification),
                        "definition": normalize_text(row["api_definition"]),
                    }
                )
        return documents

    specifications = {
        "element": ("element_id", "element_name_raw", "competency_elements"),
        "criteria": ("criteria_id", "criteria_text_raw", "performance_criteria"),
        "ksa": ("ksa_id", "ksa_text_raw", "ksa_items"),
    }
    id_column, text_column, table = specifications[item_type]
    extra = (
        ", criteria_text_refined" if item_type == "criteria" else
        ", ksa_text_refined" if item_type == "ksa" else ""
    )
    for batch in _chunks(sorted(doc_ids)):
        placeholders = ",".join("?" for _ in batch)
        rows = source.execute(
            f"SELECT {id_column}, {text_column}{extra} FROM {table} "
            f"WHERE {id_column} IN ({placeholders})",
            batch,
        )
        for row in rows:
            combined = " ".join(str(value or "") for value in row[1:])
            documents.append(
                {
                    "doc_id": int(row[0]),
                    "source_id": int(row[0]),
                    "text": str(row[1] or ""),
                    "combined": normalize_text(combined),
                    "name": normalize_text(row[1]),
                    "classification": "",
                    "definition": "",
                }
            )
    return documents


def _rank_documents(
    item_type: str,
    documents: list[dict[str, Any]],
    phrase: str,
    tokens: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    for document in documents:
        combined = document["combined"]
        if phrase and phrase in combined:
            tier = 0
        elif tokens and all(token in combined for token in tokens):
            tier = 1
        else:
            tier = 2
        document["tier"] = tier
        document["token_coverage"] = round(
            sum(token in combined for token in tokens) / max(len(tokens), 1), 4
        )
    if not documents:
        return []
    selected_tier = min(int(document["tier"]) for document in documents)
    selected = [document for document in documents if document["tier"] == selected_tier]

    def sort_key(document: dict[str, Any]) -> tuple[Any, ...]:
        if item_type == "unit":
            if document["source_id"] == phrase or document["name"] == phrase:
                detail = 0
            elif document["name"].startswith(phrase):
                detail = 1
            elif phrase in document["name"]:
                detail = 2
            elif phrase in document["classification"]:
                detail = 3
            elif phrase in document["definition"]:
                detail = 4
            else:
                detail = 5
            return (document["tier"], detail, len(document["name"]), document["source_id"])
        if item_type == "element":
            return (document["tier"], len(document["name"]), int(document["source_id"]))
        return (document["tier"], int(document["source_id"]))

    selected.sort(key=sort_key)
    return selected[:limit]


def search_postings(
    source: sqlite3.Connection,
    index: sqlite3.Connection,
    query: str,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    phrase, tokens = query_tokens(query)
    if not tokens:
        return {"query": query, "results": [], "counts_by_type": {}}
    by_type: dict[str, list[dict[str, Any]]] = {}
    diagnostics: dict[str, Any] = {}
    for item_type in TYPE_ORDER:
        candidate_ids, posting_mode, dictionary_matches = _posting_candidates(
            index, item_type, tokens
        )
        documents = _load_documents(source, index, item_type, candidate_ids)
        ranked = _rank_documents(item_type, documents, phrase, tokens, limit)
        by_type[item_type] = ranked
        diagnostics[item_type] = {
            "posting_mode": posting_mode,
            "candidate_count": len(candidate_ids),
            "dictionary_token_matches": dictionary_matches,
            "selected_tier": ranked[0]["tier"] if ranked else None,
        }
    merged: list[dict[str, Any]] = []
    position = 0
    while len(merged) < limit:
        appended = False
        for item_type in TYPE_ORDER:
            if position < len(by_type[item_type]):
                document = by_type[item_type][position]
                merged.append(
                    {
                        "type": item_type,
                        "id": document["source_id"],
                        "text": document["text"],
                        "match_tier": document["tier"],
                        "token_coverage": document["token_coverage"],
                    }
                )
                appended = True
                if len(merged) == limit:
                    break
        if not appended:
            break
        position += 1
    return {
        "query": query,
        "normalized_query": phrase,
        "query_tokens": tokens,
        "results": merged,
        "counts_by_type": {
            item_type: sum(result["type"] == item_type for result in merged)
            for item_type in TYPE_ORDER
        },
        "diagnostics": diagnostics,
    }


def _result_keys(results: list[dict[str, Any]]) -> list[str]:
    return [f"{row.get('type')}:{row.get('id')}" for row in results]


def _load_candidate_pack(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("candidate pack does not contain candidates")
    return payload, [dict(candidate) for candidate in candidates]


def _reference_results(source_path: Path, candidates: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    previous_path = os.environ.get("NCS_DB_PATH")
    previous_read_only = os.environ.get("NCS_MCP_READ_ONLY")
    os.environ["NCS_DB_PATH"] = str(source_path)
    os.environ["NCS_MCP_READ_ONLY"] = "1"
    try:
        from ncs_mcp.server import search_ncs

        records: dict[str, Any] = {}
        latencies: list[float] = []
        for candidate in candidates:
            started = time.perf_counter()
            payload = search_ncs(str(candidate["query"]), scope="all", limit=limit)
            elapsed = (time.perf_counter() - started) * 1000
            latencies.append(elapsed)
            records[str(candidate["case_id"])] = {
                "keys": _result_keys(payload.get("results") or []),
                "result_count": len(payload.get("results") or []),
                "latency_ms": round(elapsed, 3),
            }
        return {"records": records, "latency_ms": latency_summary(latencies)}
    finally:
        if previous_path is None:
            os.environ.pop("NCS_DB_PATH", None)
        else:
            os.environ["NCS_DB_PATH"] = previous_path
        if previous_read_only is None:
            os.environ.pop("NCS_MCP_READ_ONLY", None)
        else:
            os.environ["NCS_MCP_READ_ONLY"] = previous_read_only


def benchmark_queries(
    source_path: Path,
    index_path: Path,
    candidates: list[dict[str, Any]],
    *,
    runs: int,
    limit: int,
) -> dict[str, Any]:
    reference = _reference_results(source_path, candidates, limit)
    records: list[dict[str, Any]] = []
    all_latencies: list[float] = []
    query_memory_baseline = _process_memory_bytes()
    max_observed_rss = int(query_memory_baseline["rss"] or 0)
    with _readonly_connection(source_path) as source, _readonly_connection(index_path) as index:
        for candidate in candidates:
            samples: list[float] = []
            result: dict[str, Any] = {}
            for _ in range(runs):
                started = time.perf_counter()
                result = search_postings(
                    source, index, str(candidate["query"]), limit=limit
                )
                elapsed = (time.perf_counter() - started) * 1000
                samples.append(elapsed)
                all_latencies.append(elapsed)
                current_rss = int(_process_memory_bytes()["rss"] or 0)
                max_observed_rss = max(max_observed_rss, current_rss)
            candidate_keys = _result_keys(result.get("results") or [])
            reference_record = reference["records"][str(candidate["case_id"])]
            reference_keys = reference_record["keys"]
            overlap = len(set(candidate_keys) & set(reference_keys)) / max(
                len(set(reference_keys)), 1
            )
            lexical = [
                float(row.get("token_coverage") or 0.0)
                for row in result.get("results") or []
            ]
            records.append(
                {
                    "case_id": candidate["case_id"],
                    "query": candidate["query"],
                    "tags": candidate.get("tags") or [],
                    "evaluation_status": candidate.get("evaluation_status"),
                    "result_count": len(candidate_keys),
                    "zero_hit": not candidate_keys,
                    "latency_ms": latency_summary(samples),
                    "top10_overlap_with_current": round(overlap, 4),
                    "top10_candidate_keys": candidate_keys,
                    "top10_current_keys": reference_keys,
                    "mean_query_token_coverage": round(
                        sum(lexical) / max(len(lexical), 1), 4
                    ),
                    "counts_by_type": result.get("counts_by_type"),
                    "diagnostics": result.get("diagnostics"),
                }
            )
    overlaps = [float(record["top10_overlap_with_current"]) for record in records]
    lexical_scores = [float(record["mean_query_token_coverage"]) for record in records]

    def group_summary(predicate: Any) -> dict[str, Any]:
        selected = [record for record in records if predicate(record)]
        return {
            "case_count": len(selected),
            "zero_hit_count": sum(bool(record["zero_hit"]) for record in selected),
            "case_ids": [record["case_id"] for record in selected],
            "queries": [record["query"] for record in selected],
        }

    two_syllable = group_summary(
        lambda record: "two_syllable" in record["tags"]
        or (
            len(normalize_text(record["query"]).replace(" ", "")) == 2
            and " " not in normalize_text(record["query"])
        )
    )
    variants = group_summary(
        lambda record: bool(re.search(r"[^\w\s]|\s{2,}|\t", str(record["query"])))
        or any("punct" in str(tag) or "spacing" in str(tag) for tag in record["tags"])
    )
    return {
        "runs_per_query": runs,
        "limit": limit,
        "query_count": len(records),
        "latency_ms_across_calls": latency_summary(all_latencies),
        "zero_hit_count": sum(bool(record["zero_hit"]) for record in records),
        "zero_hit_rate": round(
            sum(bool(record["zero_hit"]) for record in records) / max(len(records), 1), 4
        ),
        "mean_top10_overlap_with_current": round(
            sum(overlaps) / max(len(overlaps), 1), 4
        ),
        "minimum_top10_overlap_with_current": round(min(overlaps), 4) if overlaps else None,
        "mean_query_token_coverage": round(
            sum(lexical_scores) / max(len(lexical_scores), 1), 4
        ),
        "two_syllable_checks": two_syllable,
        "punctuation_spacing_checks": variants,
        "query_memory": {
            "baseline": query_memory_baseline,
            "max_observed_rss": max_observed_rss or None,
            "observed_rss_delta_bytes": (
                max(0, max_observed_rss - int(query_memory_baseline["rss"] or 0))
                if max_observed_rss and query_memory_baseline["rss"] is not None
                else None
            ),
        },
        "current_reference": {
            "contract": "ncs_mcp.server.search_ncs",
            "database": "same_extracted_compact_snapshot",
            "latency_ms": reference["latency_ms"],
        },
        "records": records,
    }


def promotion_decision(
    build: dict[str, Any],
    benchmark: dict[str, Any],
    snapshot_bytes: int,
    *,
    lazy_tier_p50_ms: float = LAZY_TIER_P50_MS,
) -> dict[str, Any]:
    p50 = benchmark["latency_ms_across_calls"]["p50"]
    p95 = benchmark["latency_ms_across_calls"]["p95"]
    projected = snapshot_bytes + int(build["index_bytes"])
    checks = {
        "full_build": build["build_mode"] == "full_build",
        "index_at_or_below_30mb": int(build["index_bytes"]) <= INDEX_TARGET_BYTES,
        "projected_snapshot_below_460mb_soft_cap": projected < SOFT_SNAPSHOT_CAP_BYTES,
        "projected_snapshot_below_480mb_hard_cap": projected < HARD_SNAPSHOT_CAP_BYTES,
        "p50_at_least_25_percent_faster_than_lazy_tier": (
            p50 is not None and float(p50) <= lazy_tier_p50_ms * 0.75
        ),
        "p95_at_or_below_1500_ms": p95 is not None and float(p95) <= 1500.0,
        "mean_top10_overlap_at_least_0_99": (
            float(benchmark["mean_top10_overlap_with_current"]) >= 0.99
        ),
        "minimum_top10_overlap_at_least_0_8": (
            float(benchmark["minimum_top10_overlap_with_current"]) >= 0.8
        ),
        "zero_hit_count_is_zero": int(benchmark["zero_hit_count"]) == 0,
        "two_syllable_zero_hit_count_is_zero": (
            int(benchmark["two_syllable_checks"]["zero_hit_count"]) == 0
        ),
        "punctuation_spacing_zero_hit_count_is_zero": (
            int(benchmark["punctuation_spacing_checks"]["zero_hit_count"]) == 0
        ),
    }
    promote = all(checks.values())
    return {
        "decision": "eligible_for_review" if promote else "do_not_promote",
        "automatic_promotion": False,
        "checks": checks,
        "failed_checks": [name for name, passed in checks.items() if not passed],
        "projected_snapshot_bytes": projected,
        "soft_headroom_bytes": SOFT_SNAPSHOT_CAP_BYTES - projected,
        "hard_headroom_bytes": HARD_SNAPSHOT_CAP_BYTES - projected,
        "lazy_tier_comparator": {
            "p50_ms": lazy_tier_p50_ms,
            "top10_overlap": 1.0,
            "additional_bytes": 0,
        },
        "interpretation": (
            "Candidate C may proceed to human/code review; this is not deployment approval."
            if promote
            else "Candidate C is not clearly superior to the zero-byte lazy-tier candidate."
        ),
    }


def run_experiment(
    *,
    archive_path: Path,
    manifest_path: Path,
    candidate_path: Path,
    runs: int,
    limit: int,
    sample_rows_per_type: int | None,
    lazy_tier_p50_ms: float,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidate_payload, candidates = _load_candidate_pack(candidate_path)
    temp_root: Path | None = None
    with tempfile.TemporaryDirectory(prefix="ncs_compact_postings_") as temp_name:
        temp_root = Path(temp_name)
        source_path = temp_root / str(manifest["archive_member"])
        index_path = temp_root / "token_postings.db"
        extract_started = time.perf_counter()
        with zipfile.ZipFile(archive_path) as archive:
            member = archive.getinfo(str(manifest["archive_member"]))
            if member.file_size != int(manifest["sqlite_bytes"]):
                raise ValueError("archive member size does not match manifest")
            with archive.open(member) as source, source_path.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
        extract_ms = round((time.perf_counter() - extract_started) * 1000, 3)
        build = build_index(
            source_path,
            index_path,
            sample_rows_per_type=sample_rows_per_type,
        )
        benchmark = benchmark_queries(
            source_path,
            index_path,
            candidates,
            runs=runs,
            limit=limit,
        )
        decision = promotion_decision(
            build,
            benchmark,
            int(manifest["sqlite_bytes"]),
            lazy_tier_p50_ms=lazy_tier_p50_ms,
        )
        report = {
            "schema": SCHEMA,
            "version": 1,
            "generated_at": generated_at(),
            "mode": "read_only_full_experiment" if sample_rows_per_type is None else "read_only_sample_experiment",
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "latency_caveat": "Local warm filesystem/cache measurement; not Vercel absolute latency.",
            },
            "inputs": {
                "archive_path": str(archive_path),
                "archive_bytes": archive_path.stat().st_size,
                "manifest_path": str(manifest_path),
                "snapshot_bytes": int(manifest["sqlite_bytes"]),
                "candidate_path": str(candidate_path),
                "candidate_schema": candidate_payload.get("schema"),
                "candidate_count": len(candidates),
                "candidate_status": candidate_payload.get("evaluation_contract", {}).get("status"),
                "gold_labels_present": False,
            },
            "extraction": {"elapsed_ms": extract_ms, "temporary_only": True},
            "index_build": build,
            "query_benchmark": benchmark,
            "promotion_decision": decision,
            "safety": {
                "source_database_open_mode": "mode=ro&immutable=1",
                "product_database_writes": False,
                "product_code_changes": False,
                "raw_ksa_mutation": False,
                "status_updates": False,
                "human_approval_claim": False,
                "candidate_eval_is_not_gold": True,
                "temporary_artifacts_only": True,
            },
            "commands": {
                "reproduce": (
                    "python scripts/benchmark_ncs_compact_postings.py "
                    f"--runs {runs} --limit {limit}"
                    + (
                        f" --sample-rows-per-type {sample_rows_per_type}"
                        if sample_rows_per_type is not None
                        else ""
                    )
                )
            },
        }
    report["safety"]["temporary_cleanup_verified"] = bool(
        temp_root is not None and not temp_root.exists()
    )
    return report


def render_markdown(report: dict[str, Any]) -> str:
    build = report["index_build"]
    benchmark = report["query_benchmark"]
    decision = report["promotion_decision"]
    lines = [
        "# NCS Compact Token Postings Experiment",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Build mode: `{build['build_mode']}`",
        f"- Candidate queries: `{benchmark['query_count']}` (`candidate_eval`, not gold)",
        f"- Decision: `{decision['decision']}`",
        f"- Automatic promotion: `{decision['automatic_promotion']}`",
        "",
        "## Build and Size",
        "",
        f"- Build elapsed: `{build['elapsed_ms']}` ms",
        f"- Index bytes: `{build['index_bytes']}`",
        f"- Tokens: `{build['token_count']}`",
        f"- Posting IDs: `{build['posting_id_count']}`",
        f"- Encoded bytes/posting: `{build['encoded_bytes_per_posting']}`",
        f"- SQLite bytes/posting: `{build['sqlite_bytes_per_posting']}`",
        f"- Build peak RSS delta: `{build['peak_rss_delta_bytes']}` bytes",
        f"- Projected snapshot: `{decision['projected_snapshot_bytes']}` bytes",
        f"- Soft-cap headroom: `{decision['soft_headroom_bytes']}` bytes",
        f"- Hard-cap headroom: `{decision['hard_headroom_bytes']}` bytes",
        "",
        "## Query Metrics",
        "",
        f"- Local postings p50: `{benchmark['latency_ms_across_calls']['p50']}` ms",
        f"- Local postings p95: `{benchmark['latency_ms_across_calls']['p95']}` ms",
        f"- Zero hits: `{benchmark['zero_hit_count']}` / `{benchmark['query_count']}`",
        f"- Mean Top-10 overlap with current search: `{benchmark['mean_top10_overlap_with_current']}`",
        f"- Minimum Top-10 overlap with current search: `{benchmark['minimum_top10_overlap_with_current']}`",
        f"- Mean lexical token-coverage proxy: `{benchmark['mean_query_token_coverage']}`",
        f"- Two-syllable zero hits: `{benchmark['two_syllable_checks']['zero_hit_count']}` / `{benchmark['two_syllable_checks']['case_count']}`",
        f"- Punctuation/spacing zero hits: `{benchmark['punctuation_spacing_checks']['zero_hit_count']}` / `{benchmark['punctuation_spacing_checks']['case_count']}`",
        f"- Query observed RSS delta: `{benchmark['query_memory']['observed_rss_delta_bytes']}` bytes",
        "",
        "## Promotion Checks",
        "",
        "| Check | Pass |",
        "| --- | --- |",
    ]
    for name, passed in decision["checks"].items():
        lines.append(f"| `{name}` | `{passed}` |")
    lines.extend(
        [
            "",
            f"- Interpretation: {decision['interpretation']}",
            "- Candidate-eval overlap and lexical coverage are automatic proxies, not relevance judgments.",
            "- The source snapshot was opened read-only; no product DB, server, deploy, or review status was changed.",
            f"- Temporary cleanup verified: `{report['safety']['temporary_cleanup_verified']}`",
            "",
            "## Per-query Evidence",
            "",
            "| Case | Query | Results | p50 ms | p95 ms | Top-10 overlap | Lexical proxy |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for record in benchmark["records"]:
        query = str(record["query"]).replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| `{record['case_id']}` | {query} | {record['result_count']} | "
            f"{record['latency_ms']['p50']} | {record['latency_ms']['p95']} | "
            f"{record['top10_overlap_with_current']} | {record['mean_query_token_coverage']} |"
        )
    lines.extend(
        [
            "",
            "## Reproduce",
            "",
            "```powershell",
            report["commands"]["reproduce"],
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and benchmark a temporary compact token-postings search index."
    )
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--sample-rows-per-type", type=int)
    parser.add_argument("--lazy-tier-p50-ms", type=float, default=LAZY_TIER_P50_MS)
    args = parser.parse_args(argv)
    if not 1 <= args.runs <= 20:
        parser.error("--runs must be between 1 and 20")
    if not 1 <= args.limit <= 50:
        parser.error("--limit must be between 1 and 50")
    if args.sample_rows_per_type is not None and args.sample_rows_per_type < 1:
        parser.error("--sample-rows-per-type must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_experiment(
        archive_path=args.archive.resolve(),
        manifest_path=args.manifest.resolve(),
        candidate_path=args.candidates.resolve(),
        runs=args.runs,
        limit=args.limit,
        sample_rows_per_type=args.sample_rows_per_type,
        lazy_tier_p50_ms=args.lazy_tier_p50_ms,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "build_mode": report["index_build"]["build_mode"],
                "index_bytes": report["index_build"]["index_bytes"],
                "p50_ms": report["query_benchmark"]["latency_ms_across_calls"]["p50"],
                "p95_ms": report["query_benchmark"]["latency_ms_across_calls"]["p95"],
                "decision": report["promotion_decision"]["decision"],
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
