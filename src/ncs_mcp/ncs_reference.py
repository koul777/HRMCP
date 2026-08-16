from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from ncs_mcp.db import (
    clamp_limit,
    normalize_concept_key,
    normalize_spaces,
    now_utc,
    row_to_dict,
    rows_to_dicts,
)
from ncs_mcp.mapping_policy import REVIEWED_STATUSES
from ncs_mcp.recommendation import _save_recommendation


UNIT_CODE_RE = re.compile(r"\b\d{10}(?:_\d{2}v\d+)?\b", re.IGNORECASE)
CRITERIA_SENTENCE_RE = re.compile(r"[^.\n。]{5,160}(?:할 수 있다|수 있다)[.]?", re.IGNORECASE)
TRAINING_LINE_RE = re.compile(r"^.*(?:훈련기준|교육훈련|훈련 내용|훈련내용).*$", re.MULTILINE)
TRUSTED_LINK_STATUSES = tuple(sorted(REVIEWED_STATUSES))
NCS_DERIVED_MODULE_PREFIX = "NCS-DERIVED-"
REPORT_TRAINING_MODULE_PREFIX = "REPORT-TRAINING-"
HR_LABOR_MVP_MAJOR_CODE = "02"
HR_LABOR_MVP_MIDDLE_CODE = "02"
HR_LABOR_MVP_SMALL_CODE = "02"
HR_LABOR_MVP_SUB_CODES = ("01", "02")


def hr_labor_mvp_scope() -> dict[str, Any]:
    return {
        "major_code": HR_LABOR_MVP_MAJOR_CODE,
        "middle_code": HR_LABOR_MVP_MIDDLE_CODE,
        "small_code": HR_LABOR_MVP_SMALL_CODE,
        "sub_codes": list(HR_LABOR_MVP_SUB_CODES),
        "scope_name": "인사 + 노사관계 MVP",
    }


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _summary(text: Any, limit: int = 280) -> str:
    value = normalize_spaces("" if text is None else str(text))
    return value if len(value) <= limit else f"{value[:limit].rstrip()}..."


def _token_estimate(text: str) -> int:
    return max(1, len(text) // 6)


def _confidence_grade(score: float) -> str:
    if score >= 0.75:
        return "high"
    if score >= 0.5:
        return "medium"
    if score > 0:
        return "low"
    return "insufficient"


def _is_ncs_derived_module(module: dict[str, Any]) -> bool:
    return str(module.get("learn_module_seq") or "").startswith(NCS_DERIVED_MODULE_PREFIX)


def _is_report_training_module(module: dict[str, Any]) -> bool:
    return str(module.get("learn_module_seq") or "").startswith(REPORT_TRAINING_MODULE_PREFIX)


def _link_status_clause(trust_mode: str, alias: str = "link") -> tuple[str, list[Any]]:
    if trust_mode == "all":
        return f"{alias}.review_status != 'rejected'", []
    placeholders = ",".join("?" for _ in TRUSTED_LINK_STATUSES)
    return f"{alias}.review_status IN ({placeholders})", list(TRUSTED_LINK_STATUSES)


def _normalize_code_list(*values: str | list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        raw_values = [value] if isinstance(value, str) else list(value)
        for raw in raw_values:
            for part in str(raw).split(","):
                code = part.strip()
                if code and code not in seen:
                    codes.append(code)
                    seen.add(code)
    return codes


def _scope_requested(
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    sub_codes: list[str] | tuple[str, ...] | set[str] | str | None = None,
) -> bool:
    return bool(
        major_code
        or middle_code
        or small_code
        or _normalize_code_list(sub_code, sub_codes)
    )


def _scope_payload(
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    sub_codes: list[str] | tuple[str, ...] | set[str] | str | None = None,
) -> dict[str, Any]:
    return {
        "major_code": major_code,
        "middle_code": middle_code,
        "small_code": small_code,
        "sub_codes": _normalize_code_list(sub_code, sub_codes),
    }


def _classification_filter_clauses(
    alias: str = "c",
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    sub_codes: list[str] | tuple[str, ...] | set[str] | str | None = None,
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if major_code:
        clauses.append(f"{alias}.major_code = ?")
        params.append(major_code)
    if middle_code:
        clauses.append(f"{alias}.middle_code = ?")
        params.append(middle_code)
    if small_code:
        clauses.append(f"{alias}.small_code = ?")
        params.append(small_code)
    normalized_sub_codes = _normalize_code_list(sub_code, sub_codes)
    if normalized_sub_codes:
        placeholders = ",".join("?" for _ in normalized_sub_codes)
        clauses.append(f"{alias}.sub_code IN ({placeholders})")
        params.extend(normalized_sub_codes)
    return clauses, params


def _clean_number(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    return float(match.group(0)) if match else None


class _SvgTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pages: list[dict[str, Any]] = []
        self._current_page: dict[str, Any] | None = None
        self._in_text = False
        self._text_attrs: dict[str, str] = {}
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        tag_name = tag.lower()
        if tag_name == "svg":
            self._current_page = {
                "page_no": len(self.pages) + 1,
                "width": _clean_number(attr_map.get("width") or attr_map.get("viewbox")),
                "height": _clean_number(attr_map.get("height")),
                "nodes": [],
            }
            self.pages.append(self._current_page)
        elif tag_name == "text" and self._current_page is not None:
            self._in_text = True
            self._text_attrs = attr_map
            self._text_parts = []
        elif tag_name == "tspan" and self._in_text:
            if attr_map.get("x") and not self._text_attrs.get("x"):
                self._text_attrs["x"] = attr_map["x"]
            if attr_map.get("y") and not self._text_attrs.get("y"):
                self._text_attrs["y"] = attr_map["y"]

    def handle_endtag(self, tag: str) -> None:
        tag_name = tag.lower()
        if tag_name == "text" and self._in_text and self._current_page is not None:
            text = normalize_spaces(unescape("".join(self._text_parts)))
            if text:
                self._current_page["nodes"].append(
                    {
                        "x": _clean_number(self._text_attrs.get("x")),
                        "y": _clean_number(self._text_attrs.get("y")),
                        "text": text,
                    }
                )
            self._in_text = False
            self._text_attrs = {}
            self._text_parts = []
        elif tag_name == "svg":
            self._current_page = None

    def handle_data(self, data: str) -> None:
        if self._in_text:
            self._text_parts.append(data)


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = normalize_spaces(data)
        if value:
            self.parts.append(value)


@dataclass(frozen=True)
class ParsedReferencePage:
    page_no: int
    width: float | None
    height: float | None
    text: str
    nodes: list[dict[str, Any]]


def _nodes_to_text(nodes: list[dict[str, Any]]) -> str:
    ordered = sorted(
        nodes,
        key=lambda item: (
            10**9 if item.get("y") is None else float(item["y"]),
            10**9 if item.get("x") is None else float(item["x"]),
        ),
    )
    lines: list[str] = []
    current: list[str] = []
    current_y: float | None = None
    for node in ordered:
        y_value = node.get("y")
        y = float(y_value) if y_value is not None else current_y
        if current and current_y is not None and y is not None and abs(y - current_y) > 3:
            lines.append(normalize_spaces("".join(current)))
            current = []
        current.append(str(node.get("text") or ""))
        if y is not None:
            current_y = y
    if current:
        lines.append(normalize_spaces("".join(current)))
    return "\n".join(line for line in lines if line)


def parse_reference_html(html_text: str) -> list[ParsedReferencePage]:
    parser = _SvgTextParser()
    parser.feed(html_text)
    pages: list[ParsedReferencePage] = []
    for raw_page in parser.pages:
        nodes = raw_page["nodes"]
        text = _nodes_to_text(nodes)
        if not text:
            continue
        pages.append(
            ParsedReferencePage(
                page_no=int(raw_page["page_no"]),
                width=raw_page.get("width"),
                height=raw_page.get("height"),
                text=text,
                nodes=nodes,
            )
        )
    if pages:
        return pages

    fallback = _PlainTextParser()
    fallback.feed(html_text)
    text = normalize_spaces(" ".join(fallback.parts))
    return [
        ParsedReferencePage(page_no=1, width=None, height=None, text=text, nodes=[])
    ] if text else []


def parse_reference_docx(path: Path | str) -> list[ParsedReferencePage]:
    docx_path = Path(path)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with ZipFile(docx_path) as archive:
        document_xml = archive.read("word/document.xml")
    root = ET.fromstring(document_xml)
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", ns):
        texts = [node.text for node in paragraph.findall(".//w:t", ns) if node.text]
        line = normalize_spaces("".join(texts))
        if line:
            paragraphs.append(line)
    text = "\n".join(paragraphs)
    return [
        ParsedReferencePage(
            page_no=1,
            width=None,
            height=None,
            text=text,
            nodes=[],
        )
    ] if text else []


def _chunk_page_text(
    text: str,
    *,
    min_chars: int = 500,
    max_chars: int = 1200,
) -> list[str]:
    clean = normalize_spaces(text)
    if not clean:
        return []
    if len(clean) <= max_chars:
        return [clean]
    chunks: list[str] = []
    start = 0
    while start < len(clean):
        end = min(len(clean), start + max_chars)
        if end < len(clean):
            break_at = clean.rfind(" ", start + min_chars, end)
            if break_at > start:
                end = break_at
        chunk = clean[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = max(end + 1, start + 1)
    return chunks


def import_ncs_reference_html(
    conn: sqlite3.Connection,
    input_path: Path | str,
    *,
    title: str | None = None,
    chunk_min_chars: int = 500,
    chunk_max_chars: int = 1200,
) -> dict[str, Any]:
    path = Path(input_path)
    html_text = path.read_text(encoding="utf-8-sig", errors="replace")
    source_hash = hashlib.sha256(html_text.encode("utf-8")).hexdigest()
    pages = parse_reference_html(html_text)
    timestamp = now_utc()
    document_title = title or path.stem
    existing = conn.execute(
        "SELECT document_id FROM ncs_reference_documents WHERE source_hash = ?",
        (source_hash,),
    ).fetchone()
    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO ncs_reference_documents(
                title, source_path, source_hash, source_type, import_status,
                metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'html', 'imported', ?, ?, ?)
            """,
            (
                document_title,
                str(path),
                source_hash,
                _json({"parser": "svg_text_coordinates", "chunk_min_chars": chunk_min_chars, "chunk_max_chars": chunk_max_chars}),
                timestamp,
                timestamp,
            ),
        )
        document_id = int(cur.lastrowid)
    else:
        document_id = int(existing["document_id"])
        conn.execute(
            """
            UPDATE ncs_reference_documents
            SET title = ?, source_path = ?, import_status = 'imported',
                metadata_json = ?, updated_at = ?
            WHERE document_id = ?
            """,
            (
                document_title,
                str(path),
                _json({"parser": "svg_text_coordinates", "chunk_min_chars": chunk_min_chars, "chunk_max_chars": chunk_max_chars}),
                timestamp,
                document_id,
            ),
        )
        conn.execute("DELETE FROM ncs_reference_entity_links WHERE entity_id IN (SELECT entity_id FROM ncs_reference_entities WHERE document_id = ?)", (document_id,))
        conn.execute("DELETE FROM ncs_reference_entities WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM ncs_reference_chunks WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM ncs_reference_pages WHERE document_id = ?", (document_id,))

    chunk_index = 0
    for page in pages:
        conn.execute(
            """
            INSERT INTO ncs_reference_pages(
                document_id, page_no, width, height, text, char_count,
                text_nodes_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                page.page_no,
                page.width,
                page.height,
                page.text,
                len(page.text),
                _json(page.nodes),
                timestamp,
            ),
        )
        for chunk_text in _chunk_page_text(page.text, min_chars=chunk_min_chars, max_chars=chunk_max_chars):
            conn.execute(
                """
                INSERT INTO ncs_reference_chunks(
                    document_id, chunk_index, page_start, page_end, text,
                    char_count, token_estimate, summary, location_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    chunk_index,
                    page.page_no,
                    page.page_no,
                    chunk_text,
                    len(chunk_text),
                    _token_estimate(chunk_text),
                    _summary(chunk_text),
                    _json({"page_start": page.page_no, "page_end": page.page_no}),
                    timestamp,
                ),
            )
            chunk_index += 1
    conn.execute(
        """
        UPDATE ncs_reference_documents
        SET page_count = ?, chunk_count = ?, updated_at = ?
        WHERE document_id = ?
        """,
        (len(pages), chunk_index, timestamp, document_id),
    )
    conn.commit()
    return {
        "document_id": document_id,
        "title": document_title,
        "source_path": str(path),
        "source_hash": source_hash,
        "page_count": len(pages),
        "chunk_count": chunk_index,
        "status": "imported",
    }


def import_ncs_reference_docx(
    conn: sqlite3.Connection,
    input_path: Path | str,
    *,
    title: str | None = None,
    chunk_min_chars: int = 500,
    chunk_max_chars: int = 1200,
) -> dict[str, Any]:
    path = Path(input_path)
    content = path.read_bytes()
    source_hash = hashlib.sha256(content).hexdigest()
    pages = parse_reference_docx(path)
    timestamp = now_utc()
    document_title = title or path.stem
    existing = conn.execute(
        "SELECT document_id FROM ncs_reference_documents WHERE source_hash = ?",
        (source_hash,),
    ).fetchone()
    metadata = _json(
        {
            "parser": "docx_word_xml_paragraphs",
            "chunk_min_chars": chunk_min_chars,
            "chunk_max_chars": chunk_max_chars,
        }
    )
    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO ncs_reference_documents(
                title, source_path, source_hash, source_type, import_status,
                metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'docx', 'imported', ?, ?, ?)
            """,
            (document_title, str(path), source_hash, metadata, timestamp, timestamp),
        )
        document_id = int(cur.lastrowid)
    else:
        document_id = int(existing["document_id"])
        conn.execute(
            """
            UPDATE ncs_reference_documents
            SET title = ?, source_path = ?, source_type = 'docx',
                import_status = 'imported', metadata_json = ?, updated_at = ?
            WHERE document_id = ?
            """,
            (document_title, str(path), metadata, timestamp, document_id),
        )
        conn.execute("DELETE FROM ncs_reference_entity_links WHERE entity_id IN (SELECT entity_id FROM ncs_reference_entities WHERE document_id = ?)", (document_id,))
        conn.execute("DELETE FROM ncs_reference_entities WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM ncs_reference_chunks WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM ncs_reference_pages WHERE document_id = ?", (document_id,))

    chunk_index = 0
    for page in pages:
        conn.execute(
            """
            INSERT INTO ncs_reference_pages(
                document_id, page_no, width, height, text, char_count,
                text_nodes_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                page.page_no,
                page.width,
                page.height,
                page.text,
                len(page.text),
                _json(page.nodes),
                timestamp,
            ),
        )
        for chunk_text in _chunk_page_text(page.text, min_chars=chunk_min_chars, max_chars=chunk_max_chars):
            conn.execute(
                """
                INSERT INTO ncs_reference_chunks(
                    document_id, chunk_index, page_start, page_end, text,
                    char_count, token_estimate, summary, location_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    chunk_index,
                    page.page_no,
                    page.page_no,
                    chunk_text,
                    len(chunk_text),
                    _token_estimate(chunk_text),
                    _summary(chunk_text),
                    _json({"page_start": page.page_no, "page_end": page.page_no}),
                    timestamp,
                ),
            )
            chunk_index += 1
    conn.execute(
        """
        UPDATE ncs_reference_documents
        SET page_count = ?, chunk_count = ?, updated_at = ?
        WHERE document_id = ?
        """,
        (len(pages), chunk_index, timestamp, document_id),
    )
    conn.commit()
    return {
        "document_id": document_id,
        "title": document_title,
        "source_path": str(path),
        "source_hash": source_hash,
        "page_count": len(pages),
        "chunk_count": chunk_index,
        "status": "imported",
    }


def _context(text: str, start: int, end: int, limit: int = 260) -> str:
    half = max(40, limit // 2)
    left = max(0, start - half)
    right = min(len(text), end + half)
    return _summary(text[left:right], limit)


def _iter_occurrences(text: str, term: str) -> list[tuple[int, int]]:
    if not term:
        return []
    lowered = text.lower()
    needle = term.lower()
    positions: list[tuple[int, int]] = []
    start = 0
    while True:
        found = lowered.find(needle, start)
        if found < 0:
            break
        positions.append((found, found + len(term)))
        start = found + max(1, len(term))
    return positions


def _dictionary_terms(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    terms: dict[str, list[tuple[str, str]]] = {
        "unit_name": [],
        "element_name": [],
        "performance_criteria": [],
        "ksa": [],
    }
    seen: set[tuple[str, str]] = set()
    for row in conn.execute(
        """
        SELECT unit_name_raw, unit_name_refined, api_unit_name
        FROM competency_units
        """
    ).fetchall():
        for value in [row["unit_name_raw"], row["unit_name_refined"], row["api_unit_name"]]:
            term = normalize_spaces(value or "")
            if len(term) >= 2 and ("unit_name", term) not in seen:
                terms["unit_name"].append((term, "ncs_dictionary_unit_name"))
                seen.add(("unit_name", term))
    for row in conn.execute(
        "SELECT element_name_raw, element_name_refined, api_element_name FROM competency_elements"
    ).fetchall():
        for value in [row["element_name_raw"], row["element_name_refined"], row["api_element_name"]]:
            term = normalize_spaces(value or "")
            if len(term) >= 2 and ("element_name", term) not in seen:
                terms["element_name"].append((term, "ncs_dictionary_element_name"))
                seen.add(("element_name", term))
    for row in conn.execute(
        "SELECT criteria_text_raw, criteria_text_refined FROM performance_criteria"
    ).fetchall():
        for value in [row["criteria_text_raw"], row["criteria_text_refined"]]:
            term = normalize_spaces(value or "")
            if len(term) >= 8 and ("performance_criteria", term) not in seen:
                terms["performance_criteria"].append((term, "ncs_dictionary_criteria"))
                seen.add(("performance_criteria", term))
    for row in conn.execute("SELECT ksa_text_raw, ksa_text_refined FROM ksa_items").fetchall():
        for value in [row["ksa_text_raw"], row["ksa_text_refined"]]:
            term = normalize_spaces(value or "")
            if len(term) >= 2 and ("ksa", term) not in seen:
                terms["ksa"].append((term, "ncs_dictionary_ksa"))
                seen.add(("ksa", term))
    return terms


def _unit_name_terms(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    sub_codes: list[str] | tuple[str, ...] | set[str] | str | None = None,
) -> list[tuple[str, str]]:
    clauses, params = _classification_filter_clauses(
        "c",
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        sub_codes=sub_codes,
    )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT cu.unit_code, cu.unit_name_raw, cu.unit_name_refined, cu.api_unit_name
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where}
        """,
        params,
    ).fetchall()
    terms: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        for value in [row["unit_name_raw"], row["unit_name_refined"], row["api_unit_name"]]:
            term = normalize_spaces(value or "")
            key = (row["unit_code"], term)
            if len(term) >= 2 and key not in seen:
                terms.append((term, row["unit_code"]))
                seen.add(key)
    terms.sort(key=lambda item: (-len(item[0]), item[0], item[1]))
    return terms


def _scoped_terms_for_units(
    conn: sqlite3.Connection,
    unit_codes: set[str],
) -> dict[str, list[tuple[str, str]]]:
    if not unit_codes:
        return {"element_name": [], "performance_criteria": [], "ksa": []}
    placeholders = ",".join("?" for _ in unit_codes)
    params = sorted(unit_codes)
    terms: dict[str, list[tuple[str, str]]] = {
        "element_name": [],
        "performance_criteria": [],
        "ksa": [],
    }
    seen: set[tuple[str, str]] = set()
    element_rows = conn.execute(
        f"""
        SELECT element_name_raw, element_name_refined, api_element_name
        FROM competency_elements
        WHERE unit_code IN ({placeholders})
        """,
        params,
    ).fetchall()
    for row in element_rows:
        for value in [row["element_name_raw"], row["element_name_refined"], row["api_element_name"]]:
            term = normalize_spaces(value or "")
            key = ("element_name", term)
            if len(term) >= 2 and key not in seen:
                terms["element_name"].append((term, "ncs_scoped_element_name"))
                seen.add(key)
    criteria_rows = conn.execute(
        f"""
        SELECT pc.criteria_text_raw, pc.criteria_text_refined
        FROM performance_criteria pc
        JOIN competency_elements ce ON ce.element_id = pc.element_id
        WHERE ce.unit_code IN ({placeholders})
        """,
        params,
    ).fetchall()
    for row in criteria_rows:
        for value in [row["criteria_text_raw"], row["criteria_text_refined"]]:
            term = normalize_spaces(value or "")
            key = ("performance_criteria", term)
            if len(term) >= 8 and key not in seen:
                terms["performance_criteria"].append((term, "ncs_scoped_criteria"))
                seen.add(key)
    ksa_rows = conn.execute(
        f"""
        SELECT ki.ksa_text_raw, ki.ksa_text_refined
        FROM ksa_items ki
        JOIN competency_elements ce ON ce.element_id = ki.element_id
        WHERE ce.unit_code IN ({placeholders})
        """,
        params,
    ).fetchall()
    for row in ksa_rows:
        for value in [row["ksa_text_raw"], row["ksa_text_refined"]]:
            term = normalize_spaces(value or "")
            key = ("ksa", term)
            if len(term) >= 2 and key not in seen:
                terms["ksa"].append((term, "ncs_scoped_ksa"))
                seen.add(key)
    for values in terms.values():
        values.sort(key=lambda item: (-len(item[0]), item[0]))
    return terms


def _insert_entity(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    chunk_id: int,
    page_no: int | None,
    entity_type: str,
    entity_text: str,
    start_offset: int | None,
    end_offset: int | None,
    extraction_method: str,
    confidence_score: float,
    evidence_summary: str,
    metadata: dict[str, Any] | None = None,
) -> bool:
    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO ncs_reference_entities(
            document_id, chunk_id, page_no, entity_type, entity_text,
            normalized_text, start_offset, end_offset, extraction_method,
            confidence_score, evidence_summary, metadata_json, review_status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?)
        """,
        (
            document_id,
            chunk_id,
            page_no,
            entity_type,
            entity_text,
            normalize_concept_key(entity_text),
            start_offset,
            end_offset,
            extraction_method,
            confidence_score,
            evidence_summary,
            _json(metadata or {}),
            now_utc(),
        ),
    )
    return conn.total_changes > before


def extract_ncs_reference_entities(
    conn: sqlite3.Connection,
    *,
    document_id: int | None = None,
    limit_chunks: int | None = None,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    sub_codes: list[str] | tuple[str, ...] | set[str] | str | None = None,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if document_id is not None:
        clauses.append("document_id = ?")
        params.append(document_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_sql = "LIMIT ?" if limit_chunks is not None else ""
    if limit_chunks is not None:
        params.append(clamp_limit(limit_chunks, default=100, maximum=100000))
    chunks = conn.execute(
        f"""
        SELECT chunk_id, document_id, page_start, page_end, text
        FROM ncs_reference_chunks
        {where}
        ORDER BY document_id, chunk_index
        {limit_sql}
        """,
        params,
    ).fetchall()
    scope_active = _scope_requested(
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        sub_codes=sub_codes,
    )
    unit_terms = _unit_name_terms(
        conn,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        sub_codes=sub_codes,
    )
    scoped_cache: dict[tuple[str, ...], dict[str, list[tuple[str, str]]]] = {}
    inserted_by_type: dict[str, int] = {}
    scoped_chunks = 0
    for chunk in chunks:
        text = chunk["text"]
        chunk_unit_codes: set[str] = set()
        for match in UNIT_CODE_RE.finditer(text):
            resolved_units = _unit_rows_for_code(
                conn,
                match.group(0),
                major_code=major_code,
                middle_code=middle_code,
                small_code=small_code,
                sub_code=sub_code,
                sub_codes=sub_codes,
            )
            if scope_active and not resolved_units:
                continue
            chunk_unit_codes.update(row["unit_code"] for row in resolved_units)
            inserted = _insert_entity(
                conn,
                document_id=chunk["document_id"],
                chunk_id=chunk["chunk_id"],
                page_no=chunk["page_start"],
                entity_type="unit_code",
                entity_text=match.group(0),
                start_offset=match.start(),
                end_offset=match.end(),
                extraction_method="regex_unit_code",
                confidence_score=0.95,
                evidence_summary=_context(text, match.start(), match.end()),
            )
            if inserted:
                inserted_by_type["unit_code"] = inserted_by_type.get("unit_code", 0) + 1
        for term, unit_code in unit_terms:
            for start, end in _iter_occurrences(text, term):
                chunk_unit_codes.add(unit_code)
                inserted = _insert_entity(
                    conn,
                    document_id=chunk["document_id"],
                    chunk_id=chunk["chunk_id"],
                    page_no=chunk["page_start"],
                    entity_type="unit_name",
                    entity_text=term,
                    start_offset=start,
                    end_offset=end,
                    extraction_method="ncs_dictionary_unit_name",
                    confidence_score=0.85,
                    evidence_summary=_context(text, start, end),
                    metadata={"unit_code": unit_code},
                )
                if inserted:
                    inserted_by_type["unit_name"] = inserted_by_type.get("unit_name", 0) + 1
        dictionaries = {"element_name": [], "performance_criteria": [], "ksa": []}
        if chunk_unit_codes:
            scoped_chunks += 1
            cache_key = tuple(sorted(chunk_unit_codes))
            if cache_key not in scoped_cache:
                scoped_cache[cache_key] = _scoped_terms_for_units(conn, set(cache_key))
            dictionaries = scoped_cache[cache_key]
        for entity_type, terms in dictionaries.items():
            for term, method in terms:
                for start, end in _iter_occurrences(text, term):
                    inserted = _insert_entity(
                        conn,
                        document_id=chunk["document_id"],
                        chunk_id=chunk["chunk_id"],
                        page_no=chunk["page_start"],
                        entity_type=entity_type,
                        entity_text=term,
                        start_offset=start,
                        end_offset=end,
                        extraction_method=method,
                        confidence_score=0.8,
                        evidence_summary=_context(text, start, end),
                    )
                    if inserted:
                        inserted_by_type[entity_type] = inserted_by_type.get(entity_type, 0) + 1
        if not scope_active or chunk_unit_codes:
            for match in CRITERIA_SENTENCE_RE.finditer(text):
                entity_text = normalize_spaces(match.group(0))
                inserted = _insert_entity(
                    conn,
                    document_id=chunk["document_id"],
                    chunk_id=chunk["chunk_id"],
                    page_no=chunk["page_start"],
                    entity_type="performance_criteria",
                    entity_text=entity_text,
                    start_offset=match.start(),
                    end_offset=match.end(),
                    extraction_method="regex_criteria_sentence",
                    confidence_score=0.55,
                    evidence_summary=_context(text, match.start(), match.end()),
                )
                if inserted:
                    inserted_by_type["performance_criteria"] = inserted_by_type.get("performance_criteria", 0) + 1
            for match in TRAINING_LINE_RE.finditer(text):
                entity_text = normalize_spaces(match.group(0))
                inserted = _insert_entity(
                    conn,
                    document_id=chunk["document_id"],
                    chunk_id=chunk["chunk_id"],
                    page_no=chunk["page_start"],
                    entity_type="training_standard",
                    entity_text=entity_text,
                    start_offset=match.start(),
                    end_offset=match.end(),
                    extraction_method="regex_training_standard",
                    confidence_score=0.5,
                    evidence_summary=_context(text, match.start(), match.end()),
                )
                if inserted:
                    inserted_by_type["training_standard"] = inserted_by_type.get("training_standard", 0) + 1
    conn.commit()
    return {
        "document_id": document_id,
        "chunks_scanned": len(chunks),
        "chunks_with_scoped_units": scoped_chunks,
        "entities_inserted": sum(inserted_by_type.values()),
        "entities_by_type": dict(sorted(inserted_by_type.items())),
        "scope": _scope_payload(
            major_code=major_code,
            middle_code=middle_code,
            small_code=small_code,
            sub_code=sub_code,
            sub_codes=sub_codes,
        ),
    }


def _unit_rows_for_code(
    conn: sqlite3.Connection,
    value: str,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    sub_codes: list[str] | tuple[str, ...] | set[str] | str | None = None,
) -> list[sqlite3.Row]:
    clauses, params = _classification_filter_clauses(
        "c",
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        sub_codes=sub_codes,
    )
    scope_sql = f" AND {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"""
        SELECT cu.unit_code
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE (cu.unit_code = ? OR cu.base_unit_code = ?)
        {scope_sql}
        ORDER BY CASE WHEN cu.unit_code = ? THEN 0 ELSE 1 END, cu.unit_code DESC
        """,
        (value, value, *params, value),
    ).fetchall()


def _target_rows_for_entity(conn: sqlite3.Connection, entity: sqlite3.Row) -> list[dict[str, Any]]:
    text = normalize_spaces(entity["entity_text"])
    if entity["entity_type"] == "unit_code":
        return [
            {
                "target_type": "ncs_competency_unit",
                "target_id": row["unit_code"],
                "confidence_score": 0.95 if row["unit_code"] == text else 0.9,
                "link_method": "unit_code_exact_or_base",
            }
            for row in _unit_rows_for_code(conn, text)
        ]
    if entity["entity_type"] == "unit_name":
        rows = conn.execute(
            """
            SELECT unit_code
            FROM competency_units
            WHERE unit_name_raw = ?
               OR unit_name_refined = ?
               OR api_unit_name = ?
            ORDER BY unit_code DESC
            """,
            (text, text, text),
        ).fetchall()
        return [
            {
                "target_type": "ncs_competency_unit",
                "target_id": row["unit_code"],
                "confidence_score": 0.85,
                "link_method": "unit_name_dictionary",
            }
            for row in rows
        ]
    if entity["entity_type"] == "element_name":
        rows = conn.execute(
            """
            SELECT element_id
            FROM competency_elements
            WHERE element_name_raw = ?
               OR element_name_refined = ?
               OR api_element_name = ?
            ORDER BY element_id
            """,
            (text, text, text),
        ).fetchall()
        return [
            {
                "target_type": "ncs_competency_element",
                "target_id": str(row["element_id"]),
                "confidence_score": 0.82,
                "link_method": "element_name_dictionary",
            }
            for row in rows
        ]
    if entity["entity_type"] == "performance_criteria":
        rows = conn.execute(
            """
            SELECT criteria_id
            FROM performance_criteria
            WHERE criteria_text_raw = ?
               OR criteria_text_refined = ?
            ORDER BY CASE WHEN criteria_text_raw = ? THEN 0 ELSE 1 END, criteria_id
            LIMIT 20
            """,
            (text, text, text),
        ).fetchall()
        return [
            {
                "target_type": "performance_criteria",
                "target_id": str(row["criteria_id"]),
                "confidence_score": 0.75,
                "link_method": "criteria_text_match",
            }
            for row in rows
        ]
    if entity["entity_type"] == "ksa":
        rows = conn.execute(
            """
            SELECT ksa_id
            FROM ksa_items
            WHERE ksa_text_raw = ?
               OR ksa_text_refined = ?
            ORDER BY ksa_id
            LIMIT 100
            """,
            (text, text),
        ).fetchall()
        return [
            {
                "target_type": "ksa_item",
                "target_id": str(row["ksa_id"]),
                "confidence_score": 0.78,
                "link_method": "ksa_text_dictionary",
            }
            for row in rows
        ]
    return []


def _scope_unit_codes(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    sub_codes: list[str] | tuple[str, ...] | set[str] | str | None = None,
) -> set[str] | None:
    if not _scope_requested(
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        sub_codes=sub_codes,
    ):
        return None
    clauses, params = _classification_filter_clauses(
        "c",
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        sub_codes=sub_codes,
    )
    rows = conn.execute(
        f"""
        SELECT cu.unit_code
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE {' AND '.join(clauses)}
        """,
        params,
    ).fetchall()
    return {row["unit_code"] for row in rows}


def _target_unit_code(conn: sqlite3.Connection, target: dict[str, Any]) -> str | None:
    target_type = target["target_type"]
    target_id = target["target_id"]
    if target_type == "ncs_competency_unit":
        return str(target_id)
    if target_type == "ncs_competency_element":
        row = conn.execute(
            "SELECT unit_code FROM competency_elements WHERE element_id = ?",
            (target_id,),
        ).fetchone()
        return row["unit_code"] if row else None
    if target_type == "performance_criteria":
        row = conn.execute(
            """
            SELECT ce.unit_code
            FROM performance_criteria pc
            JOIN competency_elements ce ON ce.element_id = pc.element_id
            WHERE pc.criteria_id = ?
            """,
            (target_id,),
        ).fetchone()
        return row["unit_code"] if row else None
    if target_type == "ksa_item":
        row = conn.execute(
            """
            SELECT ce.unit_code
            FROM ksa_items ki
            JOIN competency_elements ce ON ce.element_id = ki.element_id
            WHERE ki.ksa_id = ?
            """,
            (target_id,),
        ).fetchone()
        return row["unit_code"] if row else None
    return None


def link_reference_entities_to_ncs(
    conn: sqlite3.Connection,
    *,
    document_id: int | None = None,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    sub_codes: list[str] | tuple[str, ...] | set[str] | str | None = None,
) -> dict[str, Any]:
    clauses = ["e.review_status != 'rejected'"]
    params: list[Any] = []
    if document_id is not None:
        clauses.append("e.document_id = ?")
        params.append(document_id)
    entities = conn.execute(
        f"""
        SELECT e.*
        FROM ncs_reference_entities e
        WHERE {' AND '.join(clauses)}
        ORDER BY e.document_id, e.entity_id
        """,
        params,
    ).fetchall()
    inserted_by_target: dict[str, int] = {}
    timestamp = now_utc()
    allowed_unit_codes = _scope_unit_codes(
        conn,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        sub_codes=sub_codes,
    )
    for entity in entities:
        for target in _target_rows_for_entity(conn, entity):
            if allowed_unit_codes is not None and _target_unit_code(conn, target) not in allowed_unit_codes:
                continue
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO ncs_reference_entity_links(
                    entity_id, target_type, target_id, relation, link_method,
                    confidence_score, evidence_summary, review_status, created_at, updated_at
                ) VALUES (?, ?, ?, 'mentions', ?, ?, ?, 'candidate', ?, ?)
                """,
                (
                    entity["entity_id"],
                    target["target_type"],
                    target["target_id"],
                    target["link_method"],
                    target["confidence_score"],
                    entity["evidence_summary"],
                    timestamp,
                    timestamp,
                ),
            )
            if conn.total_changes > before:
                inserted_by_target[target["target_type"]] = inserted_by_target.get(target["target_type"], 0) + 1
    conn.commit()
    return {
        "document_id": document_id,
        "entities_scanned": len(entities),
        "links_inserted": sum(inserted_by_target.values()),
        "links_by_target": dict(sorted(inserted_by_target.items())),
        "scope": _scope_payload(
            major_code=major_code,
            middle_code=middle_code,
            small_code=small_code,
            sub_code=sub_code,
            sub_codes=sub_codes,
        ),
    }


def search_ncs_reference_chunks(
    conn: sqlite3.Connection,
    *,
    query: str,
    document_id: int | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    clauses = ["c.text LIKE ?"]
    params: list[Any] = [f"%{query}%"]
    if document_id is not None:
        clauses.append("c.document_id = ?")
        params.append(document_id)
    rows = conn.execute(
        f"""
        SELECT
            c.chunk_id, c.document_id, c.chunk_index, c.page_start, c.page_end,
            c.text, c.summary, d.title
        FROM ncs_reference_chunks c
        JOIN ncs_reference_documents d ON d.document_id = c.document_id
        WHERE {' AND '.join(clauses)}
        ORDER BY d.document_id, c.chunk_index
        LIMIT ?
        """,
        params + [clamp_limit(limit, default=10, maximum=100)],
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        lowered = row["text"].lower()
        needle = query.lower()
        start = lowered.find(needle)
        match_summary = _context(row["text"], start, start + len(query)) if start >= 0 else row["summary"]
        results.append(
            {
                "chunk_id": row["chunk_id"],
                "document_id": row["document_id"],
                "document_title": row["title"],
                "page_start": row["page_start"],
                "page_end": row["page_end"],
                "chunk_index": row["chunk_index"],
                "summary": row["summary"],
                "match_summary": match_summary,
                "location": {
                    "document_id": row["document_id"],
                    "chunk_id": row["chunk_id"],
                    "page_start": row["page_start"],
                    "page_end": row["page_end"],
                },
            }
        )
    return results


UNIT_SELECT_SQL = """
SELECT
    cu.unit_code, cu.base_unit_code, cu.unit_version,
    cu.unit_name_raw, cu.unit_name_refined, cu.unit_level_raw,
    cu.api_unit_name, cu.api_definition, cu.api_definition_refined,
    c.classification_id,
    c.major_code, c.major_name, c.middle_code, c.middle_name,
    c.small_code, c.small_name, c.sub_code, c.sub_name
FROM competency_units cu
JOIN classifications c ON c.classification_id = cu.classification_id
"""


def _unit_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "source_key": f"ncs_unit:{row['unit_code']}",
        "unit_code": row["unit_code"],
        "base_unit_code": row["base_unit_code"],
        "unit_version": row["unit_version"],
        "unit_name": row["unit_name_refined"] or row["api_unit_name"] or row["unit_name_raw"],
        "unit_name_raw": row["unit_name_raw"],
        "unit_level": row["unit_level_raw"],
        "definition": row["api_definition_refined"] or row["api_definition"],
        "classification": {
            "classification_id": row["classification_id"],
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


def resolve_ncs_unit_target(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    unit_code: str | None = None,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    sub_codes: list[str] | tuple[str, ...] | set[str] | str | None = None,
) -> dict[str, Any] | None:
    clauses, scope_params = _classification_filter_clauses(
        "c",
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        sub_codes=sub_codes,
    )
    scope_where = " AND ".join(clauses) if clauses else "1 = 1"
    scope_and = f" AND {scope_where}" if clauses else ""
    if unit_code:
        rows = conn.execute(
            UNIT_SELECT_SQL
            + f"""
            WHERE (cu.unit_code = ? OR cu.base_unit_code = ?)
              {scope_and}
            ORDER BY CASE WHEN cu.unit_code = ? THEN 0 ELSE 1 END, cu.unit_code DESC
            LIMIT 1
            """,
            (unit_code, unit_code, *scope_params, unit_code),
        ).fetchall()
        return _unit_payload(rows[0]) if rows else None
    if not query:
        return None

    normalized_query = normalize_concept_key(query)
    rows = conn.execute(
        UNIT_SELECT_SQL
        + f"""
        WHERE {scope_where}
        ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code, cu.unit_code
        """,
        scope_params,
    ).fetchall()
    scored: list[tuple[int, sqlite3.Row]] = []
    for row in rows:
        values = [
            row["unit_code"],
            row["base_unit_code"],
            row["unit_name_raw"],
            row["unit_name_refined"],
            row["api_unit_name"],
            row["api_definition"],
        ]
        normalized_values = [normalize_concept_key(value or "") for value in values]
        if normalized_query in normalized_values:
            scored.append((0, row))
        elif any(normalized_query and normalized_query in value for value in normalized_values):
            scored.append((1, row))
    if scored:
        scored.sort(key=lambda item: (item[0], item[1]["unit_code"]))
        return _unit_payload(scored[0][1])

    like = f"%{query}%"
    for sql in [
        f"""
        SELECT cu.unit_code
        FROM competency_elements ce
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE {scope_where}
          AND (ce.element_name_raw LIKE ? OR ce.element_name_refined LIKE ? OR ce.api_element_name LIKE ?)
        ORDER BY cu.unit_code
        LIMIT 1
        """,
        f"""
        SELECT cu.unit_code
        FROM performance_criteria pc
        JOIN competency_elements ce ON ce.element_id = pc.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE {scope_where}
          AND (pc.criteria_text_raw LIKE ? OR pc.criteria_text_refined LIKE ?)
        ORDER BY cu.unit_code
        LIMIT 1
        """,
        f"""
        SELECT cu.unit_code
        FROM ksa_items ki
        JOIN competency_elements ce ON ce.element_id = ki.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE {scope_where}
          AND (ki.ksa_text_raw LIKE ? OR ki.ksa_text_refined LIKE ?)
        ORDER BY cu.unit_code
        LIMIT 1
        """,
    ]:
        params = (
            (*scope_params, like, like, like)
            if "api_element_name" in sql
            else (*scope_params, like, like)
        )
        found = conn.execute(sql, params).fetchone()
        if found is not None:
            return resolve_ncs_unit_target(
                conn,
                unit_code=found["unit_code"],
                major_code=major_code,
                middle_code=middle_code,
                small_code=small_code,
                sub_code=sub_code,
                sub_codes=sub_codes,
            )
    return None


def ncs_chain_for_unit(conn: sqlite3.Connection, unit_code: str) -> dict[str, Any]:
    elements = conn.execute(
        """
        SELECT *
        FROM competency_elements
        WHERE unit_code = ?
        ORDER BY element_id
        """,
        (unit_code,),
    ).fetchall()
    result_elements: list[dict[str, Any]] = []
    concept_seen: set[int] = set()
    concepts: list[dict[str, Any]] = []
    for element in elements:
        criteria_rows = conn.execute(
            """
            SELECT *
            FROM performance_criteria
            WHERE element_id = ?
            ORDER BY CAST(criteria_no AS INTEGER), criteria_id
            """,
            (element["element_id"],),
        ).fetchall()
        ksa_rows = conn.execute(
            """
            SELECT *
            FROM ksa_items
            WHERE element_id = ?
            ORDER BY ksa_type_code, ksa_id
            """,
            (element["element_id"],),
        ).fetchall()
        concept_rows = conn.execute(
            """
            SELECT DISTINCT
                oc.concept_id, oc.concept_name, oc.concept_type, oc.definition,
                oc.definition_status, oc.relation_status, oc.review_status
            FROM ksa_items ki
            JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
            JOIN ontology_concepts oc ON oc.concept_id = kcl.concept_id
            WHERE ki.element_id = ?
            UNION
            SELECT DISTINCT
                oc.concept_id, oc.concept_name, oc.concept_type, oc.definition,
                oc.definition_status, oc.relation_status, oc.review_status
            FROM performance_criteria pc
            JOIN criteria_concept_links ccl ON ccl.criteria_id = pc.criteria_id
            JOIN ontology_concepts oc ON oc.concept_id = ccl.concept_id
            WHERE pc.element_id = ?
            ORDER BY 1
            """,
            (element["element_id"], element["element_id"]),
        ).fetchall()
        for concept in concept_rows:
            if concept["concept_id"] not in concept_seen:
                concepts.append(row_to_dict(concept) or {})
                concept_seen.add(concept["concept_id"])
        result_elements.append(
            {
                "element": row_to_dict(element),
                "performance_criteria": rows_to_dicts(criteria_rows),
                "ksa": rows_to_dicts(ksa_rows),
                "concepts": rows_to_dicts(concept_rows),
            }
        )
    return {"elements": result_elements, "concepts": concepts}


def _reference_evidence_for_unit(
    conn: sqlite3.Connection,
    unit_code: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            l.link_id, e.entity_id, e.entity_type, e.entity_text,
            l.confidence_score, l.evidence_summary,
            c.chunk_id, c.page_start, c.page_end,
            d.document_id, d.title
        FROM ncs_reference_entity_links l
        JOIN ncs_reference_entities e ON e.entity_id = l.entity_id
        JOIN ncs_reference_chunks c ON c.chunk_id = e.chunk_id
        JOIN ncs_reference_documents d ON d.document_id = e.document_id
        WHERE l.target_type = 'ncs_competency_unit'
          AND l.target_id = ?
          AND l.review_status != 'rejected'
        ORDER BY l.confidence_score DESC, l.link_id
        LIMIT ?
        """,
        (unit_code, clamp_limit(limit, default=5, maximum=50)),
    ).fetchall()
    return [
        {
            "evidence_type": "ncs_reference_html",
            "source_table": "ncs_reference_chunks",
            "source_id": str(row["chunk_id"]),
            "chunk_id": row["chunk_id"],
            "unit_code": unit_code,
            "evidence_summary": row["evidence_summary"],
            "evidence_text": row["evidence_summary"],
            "confidence_score": row["confidence_score"],
            "document_title": row["title"],
            "document_id": row["document_id"],
            "page_start": row["page_start"],
            "page_end": row["page_end"],
            "location": {
                "document_id": row["document_id"],
                "chunk_id": row["chunk_id"],
                "page_start": row["page_start"],
                "page_end": row["page_end"],
            },
        }
        for row in rows
    ]


def _module_candidates_for_unit(
    conn: sqlite3.Connection,
    *,
    unit_code: str,
    concept_ids: list[int],
    trust_mode: str,
    limit: int,
) -> list[dict[str, Any]]:
    status_sql, status_params = _link_status_clause(trust_mode, alias="link")
    candidates: dict[str, dict[str, Any]] = {}
    unit_rows = conn.execute(
        f"""
        SELECT
            lm.*, link.link_id, link.unit_code, link.link_method,
            link.confidence_score, link.evidence_text, link.review_status
        FROM learning_module_unit_links link
        JOIN ncs_learning_modules lm ON lm.learn_module_seq = link.learn_module_seq
        WHERE link.unit_code = ?
          AND {status_sql}
        ORDER BY link.confidence_score DESC, lm.learn_module_seq
        """,
        [unit_code, *status_params],
    ).fetchall()
    for row in unit_rows:
        seq = row["learn_module_seq"]
        candidates.setdefault(
            seq,
            {
                "module": {key: row[key] for key in row.keys() if key in {
                    "learn_module_seq",
                    "learn_module_name",
                    "learn_module_text",
                    "ncs_lclas_cd",
                    "ncs_lclas_name",
                    "ncs_mclas_cd",
                    "ncs_mclas_name",
                    "ncs_sclas_cd",
                    "ncs_sclas_name",
                    "ncs_subd_cd",
                    "ncs_subd_name",
                    "source_payload",
                }},
                "score": 0.0,
                "reasons": set(),
                "links": [],
            },
        )
        item = candidates[seq]
        item["score"] += 70 * float(row["confidence_score"] or 0)
        item["reasons"].add(
            "ncs_derived_unit_plan"
            if row["link_method"] == "ncs_derived_unit_plan"
            else "trusted_unit_link"
        )
        item["links"].append(
            {
                "link_id": row["link_id"],
                "link_type": "unit",
                "unit_code": row["unit_code"],
                "link_method": row["link_method"],
                "review_status": row["review_status"],
                "confidence_score": row["confidence_score"],
                "evidence_text": row["evidence_text"],
            }
        )
    if concept_ids:
        placeholders = ",".join("?" for _ in concept_ids)
        concept_status_sql, concept_status_params = _link_status_clause(trust_mode, alias="link")
        concept_rows = conn.execute(
            f"""
            SELECT
                lm.*, link.link_id, link.concept_id, link.link_method,
                link.confidence_score, link.evidence_text, link.review_status,
                oc.concept_name
            FROM learning_module_concept_links link
            JOIN ncs_learning_modules lm ON lm.learn_module_seq = link.learn_module_seq
            JOIN ontology_concepts oc ON oc.concept_id = link.concept_id
            WHERE link.concept_id IN ({placeholders})
              AND {concept_status_sql}
            ORDER BY link.confidence_score DESC, lm.learn_module_seq
            """,
            [*concept_ids, *concept_status_params],
        ).fetchall()
        for row in concept_rows:
            seq = row["learn_module_seq"]
            candidates.setdefault(
                seq,
                {
                    "module": {key: row[key] for key in row.keys() if key in {
                        "learn_module_seq",
                        "learn_module_name",
                        "learn_module_text",
                        "ncs_lclas_cd",
                        "ncs_lclas_name",
                        "ncs_mclas_cd",
                        "ncs_mclas_name",
                        "ncs_sclas_cd",
                        "ncs_sclas_name",
                        "ncs_subd_cd",
                        "ncs_subd_name",
                        "source_payload",
                    }},
                    "score": 0.0,
                    "reasons": set(),
                    "links": [],
                },
            )
            item = candidates[seq]
            item["score"] += min(25.0, 12.0 * float(row["confidence_score"] or 0))
            item["reasons"].add("trusted_concept_link")
            item["links"].append(
                {
                    "link_id": row["link_id"],
                    "link_type": "concept",
                    "concept_id": row["concept_id"],
                    "concept_name": row["concept_name"],
                    "link_method": row["link_method"],
                    "review_status": row["review_status"],
                    "confidence_score": row["confidence_score"],
                    "evidence_text": row["evidence_text"],
                }
            )
    normalized = []
    for item in candidates.values():
        item["reasons"] = sorted(item["reasons"])
        normalized.append(item)
    normalized.sort(key=lambda item: (-item["score"], item["module"]["learn_module_seq"]))
    return normalized[: clamp_limit(limit, default=5, maximum=20)]


def _ncs_evidence_for_item(
    *,
    target: dict[str, Any],
    chain: dict[str, Any],
    module: dict[str, Any],
    links: list[dict[str, Any]],
    reference_evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = [
        {
            "evidence_type": "ncs_unit",
            "source_table": "competency_units",
            "source_id": target["unit_code"],
            "unit_code": target["unit_code"],
            "evidence_text": target["unit_name"],
            "evidence_summary": _summary(target.get("definition") or target["unit_name"]),
        }
    ]
    for element in chain["elements"][:3]:
        for criteria in element["performance_criteria"][:2]:
            evidence.append(
                {
                    "evidence_type": "performance_criteria",
                    "source_table": "performance_criteria",
                    "source_id": str(criteria["criteria_id"]),
                    "unit_code": target["unit_code"],
                    "evidence_text": criteria["criteria_text_raw"],
                    "evidence_summary": _summary(criteria["criteria_text_raw"]),
                }
            )
        for ksa in element["ksa"][:3]:
            evidence.append(
                {
                    "evidence_type": "ksa",
                    "source_table": "ksa_items",
                    "source_id": str(ksa["ksa_id"]),
                    "unit_code": target["unit_code"],
                    "evidence_text": ksa["ksa_text_raw"],
                    "evidence_summary": _summary(ksa["ksa_text_raw"]),
                }
            )
    for concept in chain["concepts"][:5]:
        evidence.append(
            {
                "evidence_type": "ontology_concept",
                "source_table": "ontology_concepts",
                "source_id": str(concept["concept_id"]),
                "concept_id": concept["concept_id"],
                "evidence_text": concept["concept_name"],
                "evidence_summary": _summary(concept.get("definition") or concept["concept_name"]),
            }
        )
    evidence.extend(reference_evidence[:3])
    for link in links[:5]:
        evidence.append(
            {
                "evidence_type": f"learning_module_{link['link_type']}_link",
                "source_table": "learning_module_unit_links"
                if link["link_type"] == "unit"
                else "learning_module_concept_links",
                "source_id": str(link["link_id"]),
                "unit_code": target["unit_code"] if link["link_type"] == "unit" else None,
                "concept_id": link.get("concept_id"),
                "learn_module_seq": module.get("learn_module_seq"),
                "evidence_text": link.get("evidence_text"),
                "evidence_summary": _summary(link.get("evidence_text") or link.get("link_method")),
                "confidence_score": link.get("confidence_score"),
            }
        )
    if module.get("learn_module_seq"):
        evidence.append(
            {
                "evidence_type": "learning_module",
                "source_table": "ncs_learning_modules",
                "source_id": module["learn_module_seq"],
                "learn_module_seq": module["learn_module_seq"],
                "unit_code": target["unit_code"],
                "evidence_text": module["learn_module_name"],
                "evidence_summary": _summary(module.get("learn_module_text") or module["learn_module_name"]),
            }
        )
    return evidence


def recommend_learning_modules_by_ncs(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    unit_code: str | None = None,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    sub_codes: list[str] | tuple[str, ...] | set[str] | str | None = None,
    trust_mode: str = "trusted",
    limit: int = 5,
    save: bool = True,
) -> dict[str, Any]:
    if trust_mode not in {"trusted", "all"}:
        return {"ok": False, "error": {"code": "unsupported_trust_mode", "trust_mode": trust_mode}}
    target = resolve_ncs_unit_target(
        conn,
        query=query,
        unit_code=unit_code,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        sub_codes=sub_codes,
    )
    if target is None:
        return {
            "ok": False,
            "error": {"code": "NCS_TARGET_NOT_FOUND", "message": "No NCS competency unit matched the request."},
            "query": query,
            "unit_code": unit_code,
            "scope": _scope_payload(
                major_code=major_code,
                middle_code=middle_code,
                small_code=small_code,
                sub_code=sub_code,
                sub_codes=sub_codes,
            ),
            "recommendations": [],
        }
    chain = ncs_chain_for_unit(conn, target["unit_code"])
    concept_ids = [int(concept["concept_id"]) for concept in chain["concepts"] if concept.get("concept_id") is not None]
    reference_evidence = _reference_evidence_for_unit(conn, target["unit_code"], limit=5)
    candidates = _module_candidates_for_unit(
        conn,
        unit_code=target["unit_code"],
        concept_ids=concept_ids,
        trust_mode=trust_mode,
        limit=limit,
    )
    if not candidates:
        candidates = [
            {
                "module": {
                    "learn_module_seq": None,
                    "learn_module_name": f"NCS-derived education plan: {target['unit_name']}",
                    "learn_module_text": "Use NCS performance criteria and KSA as learning objectives.",
                },
                "score": 35.0,
                "reasons": ["ncs_fallback", "trusted_learning_module_link_missing"],
                "links": [],
            }
        ]

    recommendations: list[dict[str, Any]] = []
    for rank, candidate in enumerate(candidates[: clamp_limit(limit, default=5, maximum=20)], start=1):
        module = candidate["module"]
        is_derived_module = _is_ncs_derived_module(module) or not module.get("learn_module_seq")
        is_report_training_module = _is_report_training_module(module)
        normalized_score = min(1.0, float(candidate["score"]) / 100.0)
        evidence = _ncs_evidence_for_item(
            target=target,
            chain=chain,
            module=module,
            links=candidate.get("links", []),
            reference_evidence=reference_evidence,
        )
        item = {
            "rank": rank,
            "learn_module_seq": module.get("learn_module_seq"),
            "learn_module_name": module.get("learn_module_name"),
            "learn_module_text": module.get("learn_module_text"),
            "target_ncs_unit": target,
            "ncs_chain": {
                "elements": chain["elements"],
                "concepts": chain["concepts"],
            },
            "matched_ksa_concepts": chain["concepts"],
            "ncs_reference_evidence": reference_evidence,
            "evidence": evidence,
            "confidence_score": round(normalized_score, 3),
            "confidence_grade": _confidence_grade(normalized_score),
            "recommendation_type": (
                "ncs_derived"
                if is_derived_module
                else (
                    "report_training_course"
                    if is_report_training_module
                    else "ncs_direct_learning_module"
                )
            ),
            "match": {
                "reasons": candidate.get("reasons", []),
                "raw_score": candidate["score"],
                "trust_mode": trust_mode,
                "trusted_links_used": trust_mode == "trusted",
            },
            "limitations": (
                ["No official study-module API item was available; this plan is generated from NCS criteria and KSA."]
                if _is_ncs_derived_module(module)
                else (
                    ["This is a report-derived education/training course candidate, not an official study-module API row."]
                    if is_report_training_module
                    else (
                        []
                        if module.get("learn_module_seq")
                        else ["No trusted learning-module link was available; returning an NCS-derived education plan."]
                    )
                )
            ),
            "metadata": {
                "candidate_links_used": trust_mode != "trusted",
                "used_trusted_statuses": list(TRUSTED_LINK_STATUSES) if trust_mode == "trusted" else [],
            },
        }
        recommendations.append(item)

    summary = {
        "recommended_modules_count": len([item for item in recommendations if item.get("learn_module_seq")]),
        "recommended_official_modules_count": len(
            [
                item
                for item in recommendations
                if item.get("learn_module_seq")
                and not str(item.get("learn_module_seq")).startswith(NCS_DERIVED_MODULE_PREFIX)
                and not str(item.get("learn_module_seq")).startswith(REPORT_TRAINING_MODULE_PREFIX)
            ]
        ),
        "recommended_report_training_courses_count": len(
            [
                item
                for item in recommendations
                if str(item.get("learn_module_seq") or "").startswith(REPORT_TRAINING_MODULE_PREFIX)
            ]
        ),
        "recommended_derived_plans_count": len(
            [
                item
                for item in recommendations
                if str(item.get("learn_module_seq") or "").startswith(NCS_DERIVED_MODULE_PREFIX)
                or not item.get("learn_module_seq")
            ]
        ),
        "target_unit_count": 1,
        "ontology_concepts_used": len(chain["concepts"]),
        "ncs_reference_chunks_used": len(reference_evidence),
        "candidate_links_used": trust_mode != "trusted",
    }
    audit = {
        "generated_at": now_utc(),
        "data_sources": [
            "competency_units",
            "competency_elements",
            "performance_criteria",
            "ksa_items",
            "ontology_concepts",
            "learning_module_unit_links",
            "learning_module_concept_links",
            "ncs_learning_modules",
            "ncs_reference_chunks",
        ],
        "target_unit_code": target["unit_code"],
        "scope": _scope_payload(
            major_code=major_code,
            middle_code=middle_code,
            small_code=small_code,
            sub_code=sub_code,
            sub_codes=sub_codes,
        ),
        "chunk_ids": [item["chunk_id"] for item in reference_evidence],
        "learn_module_seqs": [
            item["learn_module_seq"] for item in recommendations if item.get("learn_module_seq")
        ],
        "candidate_links_used": trust_mode != "trusted",
    }
    request_payload = {
        "query": query,
        "unit_code": unit_code,
        "major_code": major_code,
        "middle_code": middle_code,
        "small_code": small_code,
        "sub_code": sub_code,
        "sub_codes": _normalize_code_list(sub_codes),
        "trust_mode": trust_mode,
        "limit": limit,
    }
    run_id: int | None = None
    if save:
        run_id = _save_recommendation(
            conn,
            query=query or unit_code or target["unit_name"],
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
        "unit_code": unit_code,
        "recommendation_run_id": run_id,
        "target": target,
        "recommendation_summary": summary,
        "recommendations": recommendations,
        "audit": audit,
        "note": "Recommendations are NCS evidence-based education guidance, not official recognition decisions.",
    }
    payload["data"] = {
        "recommendation_run_id": run_id,
        "target": target,
        "recommendation_summary": summary,
        "recommendations": recommendations,
    }
    return payload


def recommend_education_by_concepts(
    conn: sqlite3.Connection,
    *,
    concepts: list[str] | None = None,
    query: str | None = None,
    trust_mode: str = "trusted",
    limit: int = 5,
    save: bool = True,
) -> dict[str, Any]:
    if trust_mode not in {"trusted", "all"}:
        return {"ok": False, "error": {"code": "unsupported_trust_mode", "trust_mode": trust_mode}}
    requested = [normalize_spaces(item) for item in concepts or [] if normalize_spaces(item)]
    if query:
        requested.append(normalize_spaces(query))
    if not requested:
        return {"ok": False, "error": {"code": "concept_query_required"}}
    concept_rows: list[sqlite3.Row] = []
    seen: set[int] = set()
    for text in requested:
        key = normalize_concept_key(text)
        rows = conn.execute(
            """
            SELECT DISTINCT oc.*
            FROM ontology_concepts oc
            LEFT JOIN ontology_concept_aliases alias ON alias.concept_id = oc.concept_id
            WHERE oc.normalized_key = ?
               OR alias.normalized_alias_key = ?
               OR oc.concept_name LIKE ?
               OR alias.alias_text LIKE ?
            ORDER BY oc.review_status DESC, oc.concept_name
            LIMIT 20
            """,
            (key, key, f"%{text}%", f"%{text}%"),
        ).fetchall()
        for row in rows:
            if row["concept_id"] not in seen:
                concept_rows.append(row)
                seen.add(row["concept_id"])
    if not concept_rows:
        return {"ok": False, "error": {"code": "CONCEPT_NOT_FOUND"}, "concepts": requested}
    status_sql, status_params = _link_status_clause(trust_mode, alias="link")
    placeholders = ",".join("?" for _ in concept_rows)
    module_rows = conn.execute(
        f"""
        SELECT
            lm.*, link.link_id, link.concept_id, link.confidence_score,
            link.evidence_text, link.review_status, oc.concept_name
        FROM learning_module_concept_links link
        JOIN ncs_learning_modules lm ON lm.learn_module_seq = link.learn_module_seq
        JOIN ontology_concepts oc ON oc.concept_id = link.concept_id
        WHERE link.concept_id IN ({placeholders})
          AND {status_sql}
        ORDER BY link.confidence_score DESC, lm.learn_module_seq
        LIMIT ?
        """,
        [*[row["concept_id"] for row in concept_rows], *status_params, clamp_limit(limit, default=5, maximum=20)],
    ).fetchall()
    recommendations: list[dict[str, Any]] = []
    for rank, row in enumerate(module_rows, start=1):
        seq = row["learn_module_seq"]
        if str(seq).startswith(REPORT_TRAINING_MODULE_PREFIX):
            recommendation_type = "report_training_course"
        elif str(seq).startswith(NCS_DERIVED_MODULE_PREFIX):
            recommendation_type = "ncs_derived"
        else:
            recommendation_type = "concept_learning_module"
        score = min(1.0, float(row["confidence_score"] or 0))
        evidence = [
            {
                "evidence_type": "ontology_concept",
                "source_table": "ontology_concepts",
                "source_id": str(row["concept_id"]),
                "concept_id": row["concept_id"],
                "evidence_text": row["concept_name"],
                "evidence_summary": row["concept_name"],
            },
            {
                "evidence_type": "learning_module_concept_link",
                "source_table": "learning_module_concept_links",
                "source_id": str(row["link_id"]),
                "concept_id": row["concept_id"],
                "learn_module_seq": seq,
                "evidence_text": row["evidence_text"],
                "evidence_summary": _summary(row["evidence_text"]),
                "confidence_score": row["confidence_score"],
            },
        ]
        recommendations.append(
            {
                "rank": rank,
                "learn_module_seq": seq,
                "learn_module_name": row["learn_module_name"],
                "learn_module_text": row["learn_module_text"],
                "matched_ksa_concepts": rows_to_dicts(concept_rows),
                "evidence": evidence,
                "confidence_score": score,
                "confidence_grade": _confidence_grade(score),
                "recommendation_type": recommendation_type,
                "match": {"reasons": ["trusted_concept_link"], "trust_mode": trust_mode},
                "limitations": [],
            }
        )
    if not recommendations:
        recommendations.append(
            {
                "rank": 1,
                "learn_module_seq": None,
                "learn_module_name": f"NCS-derived education plan: {', '.join(requested[:3])}",
                "learn_module_text": "Use the matched ontology concepts as learning objectives.",
                "matched_ksa_concepts": rows_to_dicts(concept_rows),
                "evidence": [
                    {
                        "evidence_type": "ontology_concept",
                        "source_table": "ontology_concepts",
                        "source_id": str(concept_rows[0]["concept_id"]),
                        "concept_id": concept_rows[0]["concept_id"],
                        "evidence_text": concept_rows[0]["concept_name"],
                        "evidence_summary": _summary(concept_rows[0]["definition"] or concept_rows[0]["concept_name"]),
                    }
                ],
                "confidence_score": 0.35,
                "confidence_grade": "low",
                "recommendation_type": "ncs_derived",
                "match": {"reasons": ["trusted_learning_module_link_missing"], "trust_mode": trust_mode},
                "limitations": ["No trusted learning-module concept link was available."],
            }
        )
    target = {
        "source_key": "ncs_concepts:" + ",".join(str(row["concept_id"]) for row in concept_rows),
        "concepts": rows_to_dicts(concept_rows),
    }
    summary = {
        "recommended_modules_count": len([item for item in recommendations if item.get("learn_module_seq")]),
        "recommended_report_training_courses_count": len(
            [
                item
                for item in recommendations
                if str(item.get("learn_module_seq") or "").startswith(REPORT_TRAINING_MODULE_PREFIX)
            ]
        ),
        "ontology_concepts_used": len(concept_rows),
        "candidate_links_used": trust_mode != "trusted",
    }
    audit = {
        "generated_at": now_utc(),
        "data_sources": [
            "ontology_concepts",
            "ontology_concept_aliases",
            "learning_module_concept_links",
            "ncs_learning_modules",
        ],
        "concept_ids": [row["concept_id"] for row in concept_rows],
        "learn_module_seqs": [
            item["learn_module_seq"] for item in recommendations if item.get("learn_module_seq")
        ],
        "candidate_links_used": trust_mode != "trusted",
    }
    run_id: int | None = None
    if save:
        run_id = _save_recommendation(
            conn,
            query=query or ", ".join(requested),
            request_payload={"query": query, "concepts": requested, "trust_mode": trust_mode, "limit": limit},
            target=target,
            summary=summary,
            recommendations=recommendations,
            audit=audit,
        )
        conn.commit()
    return {
        "ok": True,
        "query": query,
        "concepts": requested,
        "recommendation_run_id": run_id,
        "target": target,
        "recommendation_summary": summary,
        "recommendations": recommendations,
        "audit": audit,
        "data": {
            "recommendation_run_id": run_id,
            "target": target,
            "recommendation_summary": summary,
            "recommendations": recommendations,
        },
    }


def build_learning_module_links(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> dict[str, Any]:
    clauses: list[str] = ["learn_module_seq NOT LIKE ?", "learn_module_seq NOT LIKE ?"]
    params: list[Any] = [f"{NCS_DERIVED_MODULE_PREFIX}%", f"{REPORT_TRAINING_MODULE_PREFIX}%"]
    if major_code:
        clauses.append("ncs_lclas_cd = ?")
        params.append(major_code)
    if middle_code:
        clauses.append("ncs_mclas_cd = ?")
        params.append(middle_code)
    if small_code:
        clauses.append("ncs_sclas_cd = ?")
        params.append(small_code)
    if sub_code:
        clauses.append("ncs_subd_cd = ?")
        params.append(sub_code)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    module_seqs = [
        row["learn_module_seq"]
        for row in conn.execute(
            f"""
            SELECT learn_module_seq
            FROM ncs_learning_modules
            {where}
            ORDER BY learn_module_seq
            """,
            params,
        ).fetchall()
    ]
    from ncs_mcp.study_module_api import refresh_learning_module_links

    summary = refresh_learning_module_links(conn, module_seqs=module_seqs)
    conn.commit()
    return {
        "scope": {
            "major_code": major_code,
            "middle_code": middle_code,
            "small_code": small_code,
            "sub_code": sub_code,
        },
        "module_count": len(module_seqs),
        "links": summary,
        "note": "Generated links are candidates until review_status is accepted, reviewed, or human_reviewed.",
    }


@dataclass(frozen=True)
class ReportTrainingCourse:
    document_id: int
    document_title: str
    page_no: int
    chunk_id: int | None
    course_name: str
    course_text: str
    department: str
    education_level: str


def _band_lines(
    nodes: list[dict[str, Any]],
    *,
    xmin: float,
    xmax: float,
    ymin: float = 150,
    ymax: float = 1450,
) -> list[tuple[float, str]]:
    grouped: dict[int, list[tuple[float, str]]] = {}
    for node in nodes:
        x_value = node.get("x")
        y_value = node.get("y")
        text = normalize_spaces(node.get("text") or "")
        if x_value is None or y_value is None or not text:
            continue
        x = float(x_value)
        y = float(y_value)
        if xmin <= x <= xmax and ymin <= y <= ymax:
            key = int(round(y / 8) * 8)
            grouped.setdefault(key, []).append((x, text))
    lines: list[tuple[float, str]] = []
    for y, values in sorted(grouped.items()):
        text = normalize_spaces("".join(part for _, part in sorted(values)))
        if text:
            lines.append((float(y), text))
    return lines


def _course_title_groups(lines: list[tuple[float, str]]) -> list[dict[str, Any]]:
    groups: list[list[tuple[float, str]]] = []
    current: list[tuple[float, str]] = []
    for y, text in lines:
        if text in {"과목", "내용", "구분"}:
            continue
        if not current or y - current[-1][0] <= 80:
            current.append((y, text))
        else:
            groups.append(current)
            current = [(y, text)]
    if current:
        groups.append(current)
    titles: list[dict[str, Any]] = []
    for group in groups:
        name = normalize_spaces("".join(text for _, text in group))
        if len(name) < 2:
            continue
        titles.append(
            {
                "course_name": name,
                "center_y": sum(y for y, _ in group) / len(group),
            }
        )
    return titles


def _extract_report_training_courses_from_page(
    *,
    document_id: int,
    document_title: str,
    page_no: int,
    text_nodes_json: str,
    chunk_id: int | None,
) -> list[ReportTrainingCourse]:
    nodes = json.loads(text_nodes_json or "[]")
    title_groups = _course_title_groups(_band_lines(nodes, xmin=1030, xmax=1235))
    if not title_groups:
        return []
    description_lines = _band_lines(nodes, xmin=1260, xmax=1720)
    department_lines = _band_lines(nodes, xmin=640, xmax=840)
    level_lines = _band_lines(nodes, xmin=850, xmax=1025)
    centers = [float(item["center_y"]) for item in title_groups]
    courses: list[ReportTrainingCourse] = []
    for index, title in enumerate(title_groups):
        low = 150.0 if index == 0 else (centers[index - 1] + centers[index]) / 2
        high = 1450.0 if index == len(title_groups) - 1 else (centers[index] + centers[index + 1]) / 2
        course_text = normalize_spaces(" ".join(text for y, text in description_lines if low <= y < high))
        department = normalize_spaces(" ".join(text for y, text in department_lines if low <= y < high))
        education_level = normalize_spaces(" ".join(text for y, text in level_lines if low <= y < high))
        if len(course_text) < 20:
            continue
        courses.append(
            ReportTrainingCourse(
                document_id=document_id,
                document_title=document_title,
                page_no=page_no,
                chunk_id=chunk_id,
                course_name=title["course_name"],
                course_text=course_text,
                department=department,
                education_level=education_level,
            )
        )
    return courses


def _report_training_match_terms(concept_name: str) -> list[str]:
    terms: list[str] = []
    raw_terms = [concept_name, *re.split(r"[\s,;/·()]+", concept_name)]
    suffixes = ("기술", "능력", "방법", "기법", "법", "관리", "작성", "활용")
    grammar_suffixes = (
        "하고",
        "하며",
        "하여",
        "하는",
        "하려는",
        "되는",
        "에서",
        "위한",
        "대한",
        "적인",
        "적으로",
    )
    stop_terms = {
        "노력",
        "능력",
        "기술",
        "방법",
        "자세",
        "의지",
        "지식",
        "이해",
        "분석",
        "활용",
    }
    for raw in raw_terms:
        key = normalize_concept_key(raw)
        if not key or key in stop_terms or key.endswith(grammar_suffixes):
            continue
        if len(key) >= 6 and key not in terms:
            terms.append(key)
        for suffix in suffixes:
            if key.endswith(suffix) and len(key) - len(suffix) >= 4:
                stripped = key[: -len(suffix)]
                if stripped in stop_terms or stripped.endswith(grammar_suffixes):
                    continue
                if stripped not in terms:
                    terms.append(stripped)
    return terms


def _training_scope_concepts(
    conn: sqlite3.Connection,
    *,
    major_code: str | None,
    middle_code: str | None,
    small_code: str | None,
    sub_code: str | None,
    sub_codes: list[str] | tuple[str, ...] | set[str] | str | None,
) -> list[dict[str, Any]]:
    clauses, params = _classification_filter_clauses(
        "c",
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        sub_codes=sub_codes,
    )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT DISTINCT
            oc.concept_id, oc.concept_name, oc.concept_type,
            cu.unit_code, cu.unit_name_raw, c.sub_code, c.sub_name
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        JOIN competency_elements ce ON ce.unit_code = cu.unit_code
        JOIN ksa_items ki ON ki.element_id = ce.element_id
        JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
        JOIN ontology_concepts oc ON oc.concept_id = kcl.concept_id
        {where}
        UNION
        SELECT DISTINCT
            oc.concept_id, oc.concept_name, oc.concept_type,
            cu.unit_code, cu.unit_name_raw, c.sub_code, c.sub_name
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        JOIN competency_elements ce ON ce.unit_code = cu.unit_code
        JOIN performance_criteria pc ON pc.element_id = ce.element_id
        JOIN criteria_concept_links ccl ON ccl.criteria_id = pc.criteria_id
        JOIN ontology_concepts oc ON oc.concept_id = ccl.concept_id
        {where}
        """,
        [*params, *params],
    ).fetchall()
    concepts = []
    for row in rows:
        item = row_to_dict(row) or {}
        item["match_terms"] = _report_training_match_terms(item["concept_name"])
        concepts.append(item)
    return concepts


def _classification_names_for_sub_code(
    conn: sqlite3.Connection,
    *,
    major_code: str,
    middle_code: str,
    small_code: str,
    sub_code: str | None,
) -> dict[str, str | None]:
    row = conn.execute(
        """
        SELECT major_name, middle_name, small_name, sub_name
        FROM classifications
        WHERE major_code = ?
          AND middle_code = ?
          AND small_code = ?
          AND (? IS NULL OR sub_code = ?)
        ORDER BY sub_code
        LIMIT 1
        """,
        (major_code, middle_code, small_code, sub_code, sub_code),
    ).fetchone()
    if row is None:
        return {"major_name": None, "middle_name": None, "small_name": None, "sub_name": None}
    return row_to_dict(row) or {}


def build_report_training_courses(
    conn: sqlite3.Connection,
    *,
    document_id: int | None = None,
    major_code: str = HR_LABOR_MVP_MAJOR_CODE,
    middle_code: str = HR_LABOR_MVP_MIDDLE_CODE,
    small_code: str = HR_LABOR_MVP_SMALL_CODE,
    sub_code: str | None = None,
    sub_codes: list[str] | tuple[str, ...] | set[str] | str | None = HR_LABOR_MVP_SUB_CODES,
    review_status: str = "reviewed",
) -> dict[str, Any]:
    if review_status not in REVIEWED_STATUSES:
        return {
            "ok": False,
            "error": {
                "code": "unsupported_review_status",
                "allowed": sorted(REVIEWED_STATUSES),
            },
        }
    doc_filter = "AND d.document_id = ?" if document_id is not None else ""
    doc_params: list[Any] = [document_id] if document_id is not None else []
    pages = conn.execute(
        f"""
        SELECT d.document_id, d.title, p.page_no, p.text_nodes_json
        FROM ncs_reference_pages p
        JOIN ncs_reference_documents d ON d.document_id = p.document_id
        WHERE d.source_type = 'html'
          AND p.text LIKE '%교육훈련과정%'
          {doc_filter}
        ORDER BY d.document_id, p.page_no
        """,
        doc_params,
    ).fetchall()
    chunk_rows = conn.execute(
        """
        SELECT document_id, chunk_id, page_start, page_end
        FROM ncs_reference_chunks
        WHERE (? IS NULL OR document_id = ?)
        """,
        (document_id, document_id),
    ).fetchall()
    chunk_by_page: dict[tuple[int, int], int] = {}
    for row in chunk_rows:
        if row["page_start"] is None or row["page_end"] is None:
            continue
        for page_no in range(int(row["page_start"]), int(row["page_end"]) + 1):
            chunk_by_page.setdefault((int(row["document_id"]), page_no), int(row["chunk_id"]))

    concepts = _training_scope_concepts(
        conn,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        sub_codes=sub_codes,
    )
    timestamp = now_utc()
    conn.execute(
        "DELETE FROM learning_module_concept_links WHERE learn_module_seq LIKE ?",
        (f"{REPORT_TRAINING_MODULE_PREFIX}%",),
    )
    conn.execute(
        "DELETE FROM learning_module_unit_links WHERE learn_module_seq LIKE ?",
        (f"{REPORT_TRAINING_MODULE_PREFIX}%",),
    )
    courses_by_seq: dict[str, ReportTrainingCourse] = {}
    for page in pages:
        for course in _extract_report_training_courses_from_page(
            document_id=page["document_id"],
            document_title=page["title"],
            page_no=page["page_no"],
            text_nodes_json=page["text_nodes_json"],
            chunk_id=chunk_by_page.get((page["document_id"], page["page_no"])),
        ):
            dedupe_key = normalize_concept_key(f"{course.course_name}|{course.course_text}")
            seq_hash = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:16]
            courses_by_seq.setdefault(f"{REPORT_TRAINING_MODULE_PREFIX}{seq_hash}", course)

    class_names = _classification_names_for_sub_code(
        conn,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=None,
    )
    modules_upserted = 0
    concept_links_upserted = 0
    unit_links_upserted = 0
    examples: list[dict[str, Any]] = []
    for module_seq, course in sorted(courses_by_seq.items()):
        source_payload = {
            "source_type": "report_training_course",
            "official_study_module": False,
            "document_id": course.document_id,
            "document_title": course.document_title,
            "page_no": course.page_no,
            "chunk_id": course.chunk_id,
            "course_name": course.course_name,
            "department": course.department,
            "education_level": course.education_level,
            "scope": _scope_payload(
                major_code=major_code,
                middle_code=middle_code,
                small_code=small_code,
                sub_code=sub_code,
                sub_codes=sub_codes,
            ),
        }
        conn.execute(
            """
            INSERT INTO ncs_learning_modules(
                learn_module_seq, learn_module_name, learn_module_text,
                ncs_lclas_cd, ncs_lclas_name, ncs_mclas_cd, ncs_mclas_name,
                ncs_sclas_cd, ncs_sclas_name, ncs_subd_cd, ncs_subd_name,
                source_payload, api_fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            ON CONFLICT(learn_module_seq) DO UPDATE SET
                learn_module_name = excluded.learn_module_name,
                learn_module_text = excluded.learn_module_text,
                ncs_lclas_cd = excluded.ncs_lclas_cd,
                ncs_lclas_name = excluded.ncs_lclas_name,
                ncs_mclas_cd = excluded.ncs_mclas_cd,
                ncs_mclas_name = excluded.ncs_mclas_name,
                ncs_sclas_cd = excluded.ncs_sclas_cd,
                ncs_sclas_name = excluded.ncs_sclas_name,
                source_payload = excluded.source_payload,
                api_fetched_at = excluded.api_fetched_at
            """,
            (
                module_seq,
                f"보고서 교육훈련과정 - {course.course_name}",
                course.course_text,
                major_code,
                class_names.get("major_name"),
                middle_code,
                class_names.get("middle_name"),
                small_code,
                class_names.get("small_name"),
                _json(source_payload),
                timestamp,
            ),
        )
        modules_upserted += 1
        text_key = normalize_concept_key(f"{course.course_name} {course.course_text}")
        matched_concepts: dict[int, dict[str, Any]] = {}
        unit_match_counts: dict[str, dict[str, Any]] = {}
        for concept in concepts:
            matched_term = next((term for term in concept["match_terms"] if term in text_key), None)
            if not matched_term:
                continue
            concept_id = int(concept["concept_id"])
            matched_concepts.setdefault(concept_id, concept)
            unit_code = concept["unit_code"]
            item = unit_match_counts.setdefault(
                unit_code,
                {
                    "unit_code": unit_code,
                    "unit_name": concept["unit_name_raw"],
                    "sub_code": concept["sub_code"],
                    "sub_name": concept["sub_name"],
                    "count": 0,
                },
            )
            item["count"] += 1
            conn.execute(
                """
                INSERT INTO learning_module_concept_links(
                    learn_module_seq, concept_id, link_method, confidence_score,
                    evidence_text, review_status, created_at, updated_at
                ) VALUES (?, ?, 'reference_report_training_course_concept', 0.6, ?, ?, ?, ?)
                ON CONFLICT(learn_module_seq, concept_id, link_method) DO UPDATE SET
                    confidence_score = excluded.confidence_score,
                    evidence_text = excluded.evidence_text,
                    review_status = excluded.review_status,
                    updated_at = excluded.updated_at
                """,
                (
                    module_seq,
                    concept_id,
                    f"Report training course '{course.course_name}' mentions concept term '{matched_term}'.",
                    review_status,
                    timestamp,
                    timestamp,
                ),
            )
        concept_links_upserted += len(matched_concepts)
        for unit_code, item in unit_match_counts.items():
            unit_name_key = normalize_concept_key(item["unit_name"] or "")
            exact_unit = bool(unit_name_key and unit_name_key in text_key)
            if not exact_unit and item["count"] < 2:
                continue
            method = (
                "reference_report_training_course_unit_exact"
                if exact_unit
                else "reference_report_training_course_concept_overlap"
            )
            confidence = 0.7 if exact_unit else min(0.65, 0.35 + 0.08 * int(item["count"]))
            conn.execute(
                """
                INSERT INTO learning_module_unit_links(
                    learn_module_seq, unit_code, link_method, confidence_score,
                    evidence_text, review_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'auto_linked', ?, ?)
                ON CONFLICT(learn_module_seq, unit_code, link_method) DO UPDATE SET
                    confidence_score = excluded.confidence_score,
                    evidence_text = excluded.evidence_text,
                    updated_at = excluded.updated_at
                """,
                (
                    module_seq,
                    unit_code,
                    method,
                    confidence,
                    f"Report training course '{course.course_name}' matched {item['count']} NCS ontology concepts.",
                    timestamp,
                    timestamp,
                ),
            )
            unit_links_upserted += 1
        if unit_match_counts:
            dominant = sorted(
                unit_match_counts.values(),
                key=lambda item: (-int(item["count"]), item["sub_code"] or "", item["unit_code"]),
            )[0]
            sub_names = _classification_names_for_sub_code(
                conn,
                major_code=major_code,
                middle_code=middle_code,
                small_code=small_code,
                sub_code=dominant["sub_code"],
            )
            conn.execute(
                """
                UPDATE ncs_learning_modules
                SET ncs_subd_cd = ?,
                    ncs_subd_name = ?
                WHERE learn_module_seq = ?
                """,
                (dominant["sub_code"], sub_names.get("sub_name"), module_seq),
            )
        if len(examples) < 10:
            examples.append(
                {
                    "learn_module_seq": module_seq,
                    "learn_module_name": f"보고서 교육훈련과정 - {course.course_name}",
                    "page_no": course.page_no,
                    "matched_concepts": len(matched_concepts),
                    "matched_units": len(unit_match_counts),
                }
            )
    conn.commit()
    return {
        "ok": True,
        "scope": _scope_payload(
            major_code=major_code,
            middle_code=middle_code,
            small_code=small_code,
            sub_code=sub_code,
            sub_codes=sub_codes,
        ),
        "document_id": document_id,
        "pages_scanned": len(pages),
        "report_training_courses_upserted": modules_upserted,
        "concept_links_upserted": concept_links_upserted,
        "unit_links_upserted": unit_links_upserted,
        "examples": examples,
        "note": "REPORT-TRAINING rows are report-derived education candidates, not official NCS study-module API rows.",
    }


def _derived_plan_text(unit: sqlite3.Row, chain: dict[str, Any]) -> str:
    lines = [
        f"NCS-derived education plan for {unit['unit_name_raw']} ({unit['unit_code']}).",
        "This is generated from NCS performance criteria and KSA, not from the official study-module API.",
    ]
    for element in chain["elements"]:
        element_row = element["element"]
        lines.append(f"\n[{element_row['element_no']}] {element_row['element_name_raw']}")
        criteria = [
            normalize_spaces(row["criteria_text_raw"] or "")
            for row in element["performance_criteria"][:5]
            if normalize_spaces(row["criteria_text_raw"] or "")
        ]
        if criteria:
            lines.append("수행준거: " + " / ".join(criteria))
        ksa_by_type: dict[str, list[str]] = {}
        for ksa in element["ksa"]:
            text = normalize_spaces(ksa["ksa_text_raw"] or "")
            if not text:
                continue
            ksa_by_type.setdefault(ksa["ksa_type_name"] or "KSA", []).append(text)
        for ksa_type, values in ksa_by_type.items():
            lines.append(f"{ksa_type}: " + ", ".join(values[:8]))
    return "\n".join(lines)


def _trusted_official_module_exists(conn: sqlite3.Connection, unit_code: str) -> bool:
    placeholders = ",".join("?" for _ in TRUSTED_LINK_STATUSES)
    row = conn.execute(
        f"""
        SELECT 1
        FROM learning_module_unit_links link
        JOIN ncs_learning_modules lm ON lm.learn_module_seq = link.learn_module_seq
        WHERE link.unit_code = ?
          AND link.review_status IN ({placeholders})
          AND lm.learn_module_seq NOT LIKE ?
        LIMIT 1
        """,
        (unit_code, *TRUSTED_LINK_STATUSES, f"{NCS_DERIVED_MODULE_PREFIX}%"),
    ).fetchone()
    return row is not None


def build_ncs_derived_learning_plans(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    sub_codes: list[str] | tuple[str, ...] | set[str] | str | None = None,
    review_status: str = "reviewed",
) -> dict[str, Any]:
    if review_status not in REVIEWED_STATUSES:
        return {
            "ok": False,
            "error": {
                "code": "unsupported_review_status",
                "message": "Derived plans must be created with a trusted review status.",
                "allowed": sorted(REVIEWED_STATUSES),
            },
        }
    clauses, params = _classification_filter_clauses(
        "c",
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        sub_codes=sub_codes,
    )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    units = conn.execute(
        f"""
        SELECT
            cu.*, c.major_code, c.major_name, c.middle_code, c.middle_name,
            c.small_code, c.small_name, c.sub_code, c.sub_name
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where}
        ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code, cu.unit_code
        """,
        params,
    ).fetchall()
    timestamp = now_utc()
    created_or_updated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for unit in units:
        unit_code = unit["unit_code"]
        if _trusted_official_module_exists(conn, unit_code):
            skipped.append({"unit_code": unit_code, "reason": "trusted_official_module_exists"})
            continue
        module_seq = f"{NCS_DERIVED_MODULE_PREFIX}{unit_code}"
        chain = ncs_chain_for_unit(conn, unit_code)
        module_text = _derived_plan_text(unit, chain)
        source_payload = {
            "source_type": "ncs_derived_education_plan",
            "official_study_module": False,
            "unit_code": unit_code,
            "generated_at": timestamp,
            "source_tables": [
                "competency_units",
                "competency_elements",
                "performance_criteria",
                "ksa_items",
                "ontology_concepts",
                "ncs_reference_chunks",
            ],
            "reason": "No trusted official NCS study module link was available.",
        }
        conn.execute(
            """
            INSERT INTO ncs_learning_modules(
                learn_module_seq, learn_module_name, learn_module_text,
                ncs_lclas_cd, ncs_lclas_name, ncs_mclas_cd, ncs_mclas_name,
                ncs_sclas_cd, ncs_sclas_name, ncs_subd_cd, ncs_subd_name,
                source_payload, api_fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(learn_module_seq) DO UPDATE SET
                learn_module_name = excluded.learn_module_name,
                learn_module_text = excluded.learn_module_text,
                ncs_lclas_cd = excluded.ncs_lclas_cd,
                ncs_lclas_name = excluded.ncs_lclas_name,
                ncs_mclas_cd = excluded.ncs_mclas_cd,
                ncs_mclas_name = excluded.ncs_mclas_name,
                ncs_sclas_cd = excluded.ncs_sclas_cd,
                ncs_sclas_name = excluded.ncs_sclas_name,
                ncs_subd_cd = excluded.ncs_subd_cd,
                ncs_subd_name = excluded.ncs_subd_name,
                source_payload = excluded.source_payload,
                api_fetched_at = excluded.api_fetched_at
            """,
            (
                module_seq,
                f"NCS 기반 교육계획 - {unit['unit_name_raw']}",
                module_text,
                unit["major_code"],
                unit["major_name"],
                unit["middle_code"],
                unit["middle_name"],
                unit["small_code"],
                unit["small_name"],
                unit["sub_code"],
                unit["sub_name"],
                _json(source_payload),
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO learning_module_unit_links(
                learn_module_seq, unit_code, link_method, confidence_score,
                evidence_text, review_status, created_at, updated_at
            ) VALUES (?, ?, 'ncs_derived_unit_plan', 0.75, ?, ?, ?, ?)
            ON CONFLICT(learn_module_seq, unit_code, link_method) DO UPDATE SET
                confidence_score = excluded.confidence_score,
                evidence_text = excluded.evidence_text,
                review_status = excluded.review_status,
                updated_at = excluded.updated_at
            """,
            (
                module_seq,
                unit_code,
                "Generated from NCS performance criteria and KSA because no trusted official study module was available.",
                review_status,
                timestamp,
                timestamp,
            ),
        )
        concept_links_upserted = 0
        for concept in chain["concepts"]:
            concept_id = concept.get("concept_id")
            if concept_id is None:
                continue
            conn.execute(
                """
                INSERT INTO learning_module_concept_links(
                    learn_module_seq, concept_id, link_method, confidence_score,
                    evidence_text, review_status, created_at, updated_at
                ) VALUES (?, ?, 'ncs_derived_unit_concept', 0.75, ?, ?, ?, ?)
                ON CONFLICT(learn_module_seq, concept_id, link_method) DO UPDATE SET
                    confidence_score = excluded.confidence_score,
                    evidence_text = excluded.evidence_text,
                    review_status = excluded.review_status,
                    updated_at = excluded.updated_at
                """,
                (
                    module_seq,
                    concept_id,
                    f"Generated from NCS unit concept: {concept.get('concept_name') or concept_id}",
                    review_status,
                    timestamp,
                    timestamp,
                ),
            )
            concept_links_upserted += 1
        created_or_updated.append(
            {
                "learn_module_seq": module_seq,
                "learn_module_name": f"NCS 기반 교육계획 - {unit['unit_name_raw']}",
                "unit_code": unit_code,
                "unit_name": unit["unit_name_raw"],
                "ontology_concept_links_upserted": concept_links_upserted,
            }
        )
    conn.commit()
    return {
        "ok": True,
        "scope": _scope_payload(
            major_code=major_code,
            middle_code=middle_code,
            small_code=small_code,
            sub_code=sub_code,
            sub_codes=sub_codes,
        ),
        "derived_plans_upserted": len(created_or_updated),
        "skipped_count": len(skipped),
        "derived_plans": created_or_updated,
        "skipped": skipped,
        "note": "Derived plans are not official NCS study-module API rows; they are trusted NCS-based fallback education plans.",
    }


def review_exact_learning_module_name_links(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    sub_codes: list[str] | tuple[str, ...] | set[str] | str | None = None,
    reviewer_id: str = "ncs_learning_mvp",
) -> dict[str, Any]:
    clauses, params = _classification_filter_clauses(
        "c",
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        sub_codes=sub_codes,
    )
    where = f"AND {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT
            link.link_id, link.review_status, link.confidence_score,
            lm.learn_module_seq, lm.learn_module_name,
            cu.unit_code, cu.unit_name_raw
        FROM learning_module_unit_links link
        JOIN ncs_learning_modules lm ON lm.learn_module_seq = link.learn_module_seq
        JOIN competency_units cu ON cu.unit_code = link.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE link.link_method = 'module_text_unit_name'
          AND link.review_status NOT IN ('accepted', 'reviewed', 'human_reviewed', 'rejected')
          {where}
        ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code, cu.unit_code, lm.learn_module_seq
        """,
        params,
    ).fetchall()
    timestamp = now_utc()
    reviewed: list[dict[str, Any]] = []
    for row in rows:
        module_key = normalize_concept_key(row["learn_module_name"] or "")
        unit_key = normalize_concept_key(row["unit_name_raw"] or "")
        if not module_key or module_key != unit_key:
            continue
        conn.execute(
            """
            UPDATE learning_module_unit_links
            SET review_status = 'reviewed',
                confidence_score = ?,
                updated_at = ?
            WHERE link_id = ?
            """,
            (max(0.95, float(row["confidence_score"] or 0)), timestamp, row["link_id"]),
        )
        conn.execute(
            """
            INSERT INTO review_audit_log(
                entity_type, entity_id, action, previous_status, new_status,
                reviewer_id, notes, created_at
            ) VALUES ('learning_module_ncs_link', ?, 'review_exact_learning_module_name_links', ?, 'reviewed', ?, ?, ?)
            """,
            (
                str(row["link_id"]),
                row["review_status"],
                reviewer_id,
                "Exact learning module name matches NCS competency unit name in MVP scope.",
                timestamp,
            ),
        )
        reviewed.append(
            {
                "link_id": row["link_id"],
                "learn_module_seq": row["learn_module_seq"],
                "learn_module_name": row["learn_module_name"],
                "unit_code": row["unit_code"],
                "unit_name": row["unit_name_raw"],
            }
        )
    conn.commit()
    return {
        "scope": _scope_payload(
            major_code=major_code,
            middle_code=middle_code,
            small_code=small_code,
            sub_code=sub_code,
            sub_codes=sub_codes,
        ),
        "reviewed_count": len(reviewed),
        "reviewed_links": reviewed,
        "note": "Only exact module-name/unit-name links were marked reviewed; classification-only links remain candidates.",
    }


def ncs_learning_mvp_status(conn: sqlite3.Connection) -> dict[str, Any]:
    scope = hr_labor_mvp_scope()
    sub_codes = scope["sub_codes"]
    unit_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE c.major_code = ?
          AND c.middle_code = ?
          AND c.small_code = ?
          AND c.sub_code IN (?, ?)
        """,
        (
            scope["major_code"],
            scope["middle_code"],
            scope["small_code"],
            sub_codes[0],
            sub_codes[1],
        ),
    ).fetchone()[0]
    module_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM ncs_learning_modules
        WHERE ncs_lclas_cd = ?
          AND ncs_mclas_cd = ?
          AND ncs_sclas_cd = ?
          AND ncs_subd_cd IN (?, ?)
        """,
        (
            scope["major_code"],
            scope["middle_code"],
            scope["small_code"],
            sub_codes[0],
            sub_codes[1],
        ),
    ).fetchone()[0]
    module_kind_rows = conn.execute(
        """
        SELECT
            CASE
                WHEN learn_module_seq LIKE ? THEN 'report_training'
                WHEN learn_module_seq LIKE ? THEN 'ncs_derived'
                ELSE 'study_module_api'
            END AS module_kind,
            COUNT(*) AS count
        FROM ncs_learning_modules
        WHERE ncs_lclas_cd = ?
          AND ncs_mclas_cd = ?
          AND ncs_sclas_cd = ?
        GROUP BY module_kind
        ORDER BY module_kind
        """,
        (
            f"{REPORT_TRAINING_MODULE_PREFIX}%",
            f"{NCS_DERIVED_MODULE_PREFIX}%",
            scope["major_code"],
            scope["middle_code"],
            scope["small_code"],
        ),
    ).fetchall()
    report_linked_course_count = conn.execute(
        """
        SELECT COUNT(DISTINCT lm.learn_module_seq)
        FROM ncs_learning_modules lm
        JOIN learning_module_concept_links link ON link.learn_module_seq = lm.learn_module_seq
        WHERE lm.learn_module_seq LIKE ?
          AND lm.ncs_lclas_cd = ?
          AND lm.ncs_mclas_cd = ?
          AND lm.ncs_sclas_cd = ?
        """,
        (
            f"{REPORT_TRAINING_MODULE_PREFIX}%",
            scope["major_code"],
            scope["middle_code"],
            scope["small_code"],
        ),
    ).fetchone()[0]
    link_status_rows = conn.execute(
        """
        SELECT link.review_status, COUNT(*) AS count
        FROM learning_module_unit_links link
        JOIN competency_units cu ON cu.unit_code = link.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE c.major_code = ?
          AND c.middle_code = ?
          AND c.small_code = ?
          AND c.sub_code IN (?, ?)
        GROUP BY link.review_status
        ORDER BY link.review_status
        """,
        (
            scope["major_code"],
            scope["middle_code"],
            scope["small_code"],
            sub_codes[0],
            sub_codes[1],
        ),
    ).fetchall()
    reference_link_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM ncs_reference_entity_links link
        JOIN ncs_reference_entities e ON e.entity_id = link.entity_id
        JOIN competency_units cu ON cu.unit_code = link.target_id
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE link.target_type = 'ncs_competency_unit'
          AND c.major_code = ?
          AND c.middle_code = ?
          AND c.small_code = ?
          AND c.sub_code IN (?, ?)
        """,
        (
            scope["major_code"],
            scope["middle_code"],
            scope["small_code"],
            sub_codes[0],
            sub_codes[1],
        ),
    ).fetchone()[0]
    concept_link_status_rows = conn.execute(
        """
        SELECT link.review_status, COUNT(*) AS count
        FROM learning_module_concept_links link
        JOIN ncs_learning_modules lm ON lm.learn_module_seq = link.learn_module_seq
        WHERE lm.ncs_lclas_cd = ?
          AND lm.ncs_mclas_cd = ?
          AND lm.ncs_sclas_cd = ?
          AND lm.ncs_subd_cd IN (?, ?)
        GROUP BY link.review_status
        ORDER BY link.review_status
        """,
        (
            scope["major_code"],
            scope["middle_code"],
            scope["small_code"],
            sub_codes[0],
            sub_codes[1],
        ),
    ).fetchall()
    concept_link_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM learning_module_concept_links link
        JOIN ncs_learning_modules lm ON lm.learn_module_seq = link.learn_module_seq
        WHERE lm.ncs_lclas_cd = ?
          AND lm.ncs_mclas_cd = ?
          AND lm.ncs_sclas_cd = ?
          AND lm.ncs_subd_cd IN (?, ?)
        """,
        (
            scope["major_code"],
            scope["middle_code"],
            scope["small_code"],
            sub_codes[0],
            sub_codes[1],
        ),
    ).fetchone()[0]
    return {
        "scope": scope,
        "unit_count": int(unit_count),
        "learning_module_count": int(module_count),
        "learning_module_count_by_kind": {
            row["module_kind"]: row["count"] for row in module_kind_rows
        },
        "report_training_courses_with_concept_links": int(report_linked_course_count),
        "learning_module_unit_links_by_status": {
            row["review_status"]: row["count"] for row in link_status_rows
        },
        "learning_module_concept_link_count": int(concept_link_count),
        "learning_module_concept_links_by_status": {
            row["review_status"]: row["count"] for row in concept_link_status_rows
        },
        "ncs_reference_unit_link_count": int(reference_link_count),
    }


def review_learning_module_ncs_link(
    conn: sqlite3.Connection,
    *,
    link_id: int,
    review_status: str,
    reviewer_id: str = "mcp",
    notes: str = "",
    confidence_score: float | None = None,
) -> dict[str, Any]:
    allowed = {
        "accepted",
        "reviewed",
        "human_reviewed",
        "rejected",
        "candidate",
        "auto_candidate",
        "auto_linked",
    }
    status = review_status.strip()
    if status not in allowed:
        return {"ok": False, "error": {"code": "unsupported_review_status", "allowed": sorted(allowed)}}
    row = conn.execute(
        "SELECT * FROM learning_module_unit_links WHERE link_id = ?",
        (link_id,),
    ).fetchone()
    if row is None:
        return {"ok": False, "error": {"code": "learning_module_ncs_link_not_found", "link_id": link_id}}
    timestamp = now_utc()
    conn.execute(
        """
        UPDATE learning_module_unit_links
        SET review_status = ?,
            confidence_score = COALESCE(?, confidence_score),
            updated_at = ?
        WHERE link_id = ?
        """,
        (status, confidence_score, timestamp, link_id),
    )
    conn.execute(
        """
        INSERT INTO review_audit_log(
            entity_type, entity_id, action, previous_status, new_status,
            reviewer_id, notes, created_at
        ) VALUES ('learning_module_ncs_link', ?, 'review_learning_module_ncs_link', ?, ?, ?, ?, ?)
        """,
        (str(link_id), row["review_status"], status, reviewer_id, notes, timestamp),
    )
    updated = conn.execute(
        "SELECT * FROM learning_module_unit_links WHERE link_id = ?",
        (link_id,),
    ).fetchone()
    conn.commit()
    return {
        "ok": True,
        "link_id": link_id,
        "previous_status": row["review_status"],
        "new_status": status,
        "recommendation_eligible": status in REVIEWED_STATUSES,
        "link": row_to_dict(updated),
        "audit": {
            "data_sources": ["learning_module_unit_links", "review_audit_log"],
            "reviewer_id": reviewer_id,
            "generated_at": now_utc(),
        },
    }
