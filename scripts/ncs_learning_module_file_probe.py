from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus


ROW_RE = re.compile(r"<tr\s+rowindex=\"(?P<row_index>\d+)\"[^>]*>(?P<body>.*?)</tr>", re.I | re.S)
TD_RE = re.compile(r"<td\s+colid=\"(?P<colid>[^\"]+)\"[^>]*>(?P<value>.*?)</td>", re.I | re.S)
INPUT_RE = re.compile(
    r"<input\s+type=\"hidden\"\s+value=\"(?P<value>[^\"]*)\"\s+id=\"(?P<field>[A-Za-z]+)(?P<index>\d+)\"",
    re.I,
)
TAG_RE = re.compile(r"<[^>]+>")
SAFE_HEADER_KEYS = {
    "status_line",
    "content-type",
    "content-length",
    "content-disposition",
    "decoded_filename",
}


def _clean_html_text(value: str) -> str:
    text = TAG_RE.sub(" ", value)
    return re.sub(r"\s+", " ", text).strip()


def _extract_rows(html: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for match in ROW_RE.finditer(html):
        body = match.group("body")
        row: dict[str, Any] = {"row_index": int(match.group("row_index"))}
        for td in TD_RE.finditer(body):
            row[td.group("colid")] = _clean_html_text(td.group("value"))
        for hidden in INPUT_RE.finditer(body):
            row[hidden.group("field")] = hidden.group("value").strip()
        if row.get("fileMstky") and row.get("filedetlSeq"):
            row["download_key"] = "|".join(
                [
                    str(row.get("sysDstinCd") or ""),
                    str(row.get("fileMstky") or ""),
                    str(row.get("filedetlSeq") or ""),
                ]
            )
        rows.append(row)
    return rows


def _extract_page_index(html: str) -> dict[str, Any]:
    selected_values = [
        int(value)
        for value in re.findall(r"selectedIdx\",\s*\"(?P<value>\d+)\"", html)
    ]
    total_values = [
        int(value)
        for value in re.findall(r"totPage\s+=\s+gfn_str_parseNull\(\"(?P<value>\d+)\"\)", html)
    ]
    selected_value = max(selected_values) if selected_values else None
    total_value = max(total_values) if total_values else None
    candidates = [value for value in (selected_value, total_value) if value is not None]
    max_index = max(candidates) if candidates else None
    return {
        "page_index_max_observed": max_index,
        "page_index_source": "selectedIdx_or_totPage" if candidates else None,
        "possible_page_count_if_zero_based": (max_index + 1) if max_index is not None else None,
        "possible_page_count_if_one_based": max_index if max_index is not None else None,
        "observed_selectedIdx_values": selected_values,
        "observed_totPage_values": total_values,
    }


def _parse_headers(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    parsed: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in line:
            if line.startswith("HTTP/"):
                parsed["status_line"] = line.strip()
            continue
        key, value = line.split(":", 1)
        parsed[key.strip().lower()] = value.strip()
    disposition = parsed.get("content-disposition") or ""
    filename_match = re.search(r"filename=([^;]+)", disposition, re.I)
    if filename_match:
        parsed["decoded_filename"] = unquote_plus(filename_match.group(1).strip().strip('"'))
    return {
        key: value
        for key, value in parsed.items()
        if key in SAFE_HEADER_KEYS and value not in (None, "")
    }


def _try_pdf_page_count(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    result: dict[str, Any] = {"bytes": path.stat().st_size}
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        result["page_count"] = len(reader.pages)
        first_text_chars = 0
        for page in reader.pages[:3]:
            first_text_chars += len(page.extract_text() or "")
        result["first_three_pages_text_chars"] = first_text_chars
        result["text_extraction_note"] = "image_or_low_text_pdf" if first_text_chars == 0 else "text_extractable_sample"
    except Exception as exc:  # pragma: no cover - dependency/environment guard
        result["pdf_probe_error"] = str(exc)
    return result


def _size_estimates(sample_bytes: int | None, raw_rows: int | None, unique_rows: int | None) -> dict[str, Any]:
    if not sample_bytes:
        return {}
    mib = sample_bytes / (1024 * 1024)
    estimates: dict[str, Any] = {"sample_mib": round(mib, 2)}
    if raw_rows:
        estimates["raw_rows_at_sample_size_gib"] = round((sample_bytes * raw_rows) / (1024**3), 2)
    if unique_rows:
        estimates["unique_files_at_sample_size_gib"] = round((sample_bytes * unique_rows) / (1024**3), 2)
    return estimates


def build_probe(html_path: Path, headers_path: Path | None, sample_file: Path | None) -> dict[str, Any]:
    html = html_path.read_text(encoding="utf-8", errors="replace")
    rows = _extract_rows(html)
    page_index = _extract_page_index(html)
    raw_row_count = len(rows)
    unique_keys = sorted({row["download_key"] for row in rows if row.get("download_key")})
    duplicate_groups = Counter(row.get("download_key") for row in rows if row.get("download_key"))
    duplicate_ratio = round(len(unique_keys) / raw_row_count, 4) if raw_row_count else None
    possible_page_count = page_index.get("possible_page_count_if_zero_based")
    estimated_raw_rows = possible_page_count * raw_row_count if possible_page_count and raw_row_count else None
    estimated_unique_files = round(estimated_raw_rows * duplicate_ratio) if estimated_raw_rows and duplicate_ratio else None
    headers = _parse_headers(headers_path)
    sample_pdf = _try_pdf_page_count(sample_file)
    sample_bytes = int(sample_pdf.get("bytes") or 0) or None
    return {
        "schema": "ncs_learning_module_file_probe_v1",
        "source_url": "https://www.ncs.go.kr/unity/th03/ncsModuleFileSearch.do",
        "download_endpoint": "https://www.ncs.go.kr/unity/hth01/hth0101/downloadFile.do",
        "source_files": {
            "html": str(html_path),
            "headers": str(headers_path) if headers_path else None,
            "sample_file": str(sample_file) if sample_file else None,
        },
        "page_index": page_index,
        "first_page": {
            "raw_rows": raw_row_count,
            "unique_download_keys": len(unique_keys),
            "raw_to_unique_ratio": duplicate_ratio,
            "duplicate_download_key_counts": dict(sorted(duplicate_groups.items())),
            "sample_rows": rows[:10],
        },
        "estimated_scope": {
            "raw_rows_if_zero_based_page_index": estimated_raw_rows,
            "unique_files_if_first_page_ratio_holds": estimated_unique_files,
            "caveat": "The official page exposes a page index, not a stable bulk API contract; crawl and dedupe before download.",
        },
        "download_form": {
            "method": "POST",
            "required_fields": ["sysDstinCd", "fileMstky", "filedetlSeq", "mbrClCd", "posCd", "downlDstinCd"],
            "observed_defaults": {"mbrClCd": "10", "downlDstinCd": "02", "histYn": "N"},
            "dedupe_key": ["sysDstinCd", "fileMstky", "filedetlSeq"],
        },
        "sample_download": {
            "headers": headers,
            "pdf_probe": sample_pdf,
            "storage_estimate": _size_estimates(sample_bytes, estimated_raw_rows, estimated_unique_files),
        },
        "project_use_policy": {
            "active_recommendation_scoring": False,
            "allowed_role": "auxiliary_review_reference",
            "reason": "NCS study modules remain a legacy/reference source unless explicitly reactivated; current scoring should stay on NCS ontology, training courses, career paths, qualifications, and job-base evidence.",
        },
        "recommended_next_steps": [
            "Crawl index pages into JSONL with a delay and resume state.",
            "Deduplicate by sysDstinCd/fileMstky/filedetlSeq before downloading PDFs.",
            "Download first the HR scope or matched-unit subset for review evidence.",
            "Store PDF metadata and hashes separately from active recommendation tables.",
            "Run OCR only for selected review scopes because the sampled PDF has no extractable text on the first pages.",
        ],
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    first = report["first_page"]
    scope = report["estimated_scope"]
    sample = report["sample_download"]
    pdf = sample.get("pdf_probe") or {}
    storage = sample.get("storage_estimate") or {}
    lines = [
        "# NCS Learning Module File Probe",
        "",
        f"- Source URL: {report['source_url']}",
        f"- Download endpoint: {report['download_endpoint']}",
        f"- Observed max page index: {report['page_index'].get('page_index_max_observed')}",
        f"- First-page raw rows: {first['raw_rows']}",
        f"- First-page unique download keys: {first['unique_download_keys']}",
        f"- Estimated raw rows if zero-based page index: {scope.get('raw_rows_if_zero_based_page_index')}",
        f"- Estimated unique files if first-page ratio holds: {scope.get('unique_files_if_first_page_ratio_holds')}",
        f"- Sample bytes: {pdf.get('bytes')}",
        f"- Sample PDF pages: {pdf.get('page_count')}",
        f"- Sample first-three-pages text chars: {pdf.get('first_three_pages_text_chars')}",
        f"- Storage estimate at sample size, raw rows GiB: {storage.get('raw_rows_at_sample_size_gib')}",
        f"- Storage estimate at sample size, deduped GiB: {storage.get('unique_files_at_sample_size_gib')}",
        "",
        "## Policy",
        "",
        "- Active recommendation scoring: false",
        "- Allowed role: auxiliary review reference",
        "- Study modules should not be promoted into active scoring without an explicit reactivation decision.",
        "",
        "## Download Plan",
        "",
        "1. Crawl index pages with throttling and resume state.",
        "2. Deduplicate by `sysDstinCd`, `fileMstky`, and `filedetlSeq`.",
        "3. Download HR-scope or matched-unit files first.",
        "4. Store metadata, hashes, and OCR status separately from active recommendation evidence.",
        "5. Use OCR only for selected review scopes unless storage and runtime budget are approved.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--headers", type=Path)
    parser.add_argument("--sample-file", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    args = parser.parse_args()
    report = build_probe(args.html, args.headers, args.sample_file)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(report, args.markdown_out)


if __name__ == "__main__":
    main()
