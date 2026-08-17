from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ncs_learning_module_file_probe import _extract_page_index, _extract_rows


BASE_URL = "https://www.ncs.go.kr/unity/th03/ncsModuleFileSearch.do"


def _curl_binary() -> str:
    return shutil.which("curl.exe") or shutil.which("curl") or "curl.exe"


def _page_url(page_index: int) -> str:
    return f"{BASE_URL}?pageUseYn=Y&pageIndex={page_index}"


def _fetch_page(page_index: int, html_path: Path, *, curl_bin: str, timeout: int) -> dict[str, Any]:
    html_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        curl_bin,
        "-L",
        "--ssl-no-revoke",
        "--http1.1",
        "--silent",
        "--show-error",
        "--max-time",
        str(timeout),
        _page_url(page_index),
        "-o",
        str(html_path),
        "-w",
        "%{http_code} %{content_type} %{size_download}",
    ]
    started = time.time()
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    elapsed = round(time.time() - started, 3)
    status_line = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout.strip() else ""
    parts = status_line.split()
    http_code = parts[0] if parts else ""
    size_download = parts[-1] if len(parts) > 1 else ""
    content_type = " ".join(parts[1:-1]) if len(parts) > 2 else ""
    return {
        "page_index": page_index,
        "url": _page_url(page_index),
        "html_path": str(html_path),
        "returncode": proc.returncode,
        "http_code": http_code,
        "content_type": content_type,
        "size_download": size_download,
        "elapsed_seconds": elapsed,
        "stderr": (proc.stderr or "").strip()[:2000],
        "ok": proc.returncode == 0 and http_code == "200" and html_path.exists() and html_path.stat().st_size > 0,
    }


def _row_record(page_index: int, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "page_index": page_index,
        "row_index": row.get("row_index"),
        "rn": row.get("rn"),
        "ncs_cl_cd": row.get("ncsClCd"),
        "competency_unit_name": row.get("compeUnitNm"),
        "learn_module_seq": row.get("learnModulSeq"),
        "sys_dstin_cd": row.get("sysDstinCd"),
        "file_mstky": row.get("fileMstky"),
        "filedetl_seq": row.get("filedetlSeq"),
        "download_key": row.get("download_key"),
    }


def collect_index(
    *,
    start_page: int,
    max_pages: int,
    cache_dir: Path,
    jsonl_out: Path,
    summary_out: Path,
    delay_seconds: float,
    timeout: int,
    retries: int,
    refresh: bool,
) -> dict[str, Any]:
    curl_bin = _curl_binary()
    jsonl_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    page_results: list[dict[str, Any]] = []
    row_count = 0
    unique_keys: set[str] = set()
    max_page_index_observed: int | None = None
    with jsonl_out.open("w", encoding="utf-8") as handle:
        for offset in range(max_pages):
            page_index = start_page + offset
            html_path = cache_dir / f"ncs_module_file_search_page_{page_index:04d}.html"
            if html_path.exists() and html_path.stat().st_size > 0 and not refresh:
                fetch_result = {
                    "page_index": page_index,
                    "url": _page_url(page_index),
                    "html_path": str(html_path),
                    "returncode": 0,
                    "http_code": "cached",
                    "content_type": "text/html",
                    "size_download": str(html_path.stat().st_size),
                    "elapsed_seconds": 0.0,
                    "stderr": "",
                    "ok": True,
                }
            else:
                fetch_result = _fetch_page(page_index, html_path, curl_bin=curl_bin, timeout=timeout)
                retry_attempts_used = 0
                for retry_index in range(retries):
                    if fetch_result["ok"]:
                        break
                    retry_attempts_used = retry_index + 1
                    time.sleep(min(2.0 * retry_attempts_used, 10.0))
                    fetch_result = _fetch_page(page_index, html_path, curl_bin=curl_bin, timeout=timeout)
                fetch_result["retry_attempts"] = retry_attempts_used
            if not fetch_result["ok"]:
                page_results.append({**fetch_result, "row_count": 0})
                break
            html = html_path.read_text(encoding="utf-8", errors="replace")
            page_meta = _extract_page_index(html)
            if page_meta.get("page_index_max_observed") is not None:
                max_page_index_observed = max(
                    max_page_index_observed or 0,
                    int(page_meta["page_index_max_observed"]),
                )
            rows = _extract_rows(html)
            for row in rows:
                record = _row_record(page_index, row)
                if record.get("download_key"):
                    unique_keys.add(str(record["download_key"]))
                row_count += 1
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            page_results.append({**fetch_result, "row_count": len(rows), "page_meta": page_meta})
            if delay_seconds and fetch_result.get("http_code") != "cached" and offset < max_pages - 1:
                time.sleep(delay_seconds)
    summary = {
        "schema": "ncs_learning_module_file_index_v1",
        "source_url": BASE_URL,
        "start_page": start_page,
        "max_pages_requested": max_pages,
        "pages_processed": len(page_results),
        "pages_ok": sum(1 for item in page_results if item.get("ok")),
        "row_count": row_count,
        "unique_download_key_count": len(unique_keys),
        "duplicate_ratio": round(len(unique_keys) / row_count, 4) if row_count else None,
        "max_page_index_observed": max_page_index_observed,
        "jsonl_out": str(jsonl_out),
        "cache_dir": str(cache_dir),
        "curl_binary": curl_bin,
        "delay_seconds": delay_seconds,
        "timeout": timeout,
        "retries": retries,
        "page_results": page_results,
    }
    summary_out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-page", type=int, default=0)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--cache-dir", type=Path, default=Path("tmp/ncs_learning_module_file_pages"))
    parser.add_argument("--jsonl-out", type=Path, required=True)
    parser.add_argument("--summary-out", type=Path, required=True)
    parser.add_argument("--delay-seconds", type=float, default=0.5)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    summary = collect_index(
        start_page=args.start_page,
        max_pages=args.max_pages,
        cache_dir=args.cache_dir,
        jsonl_out=args.jsonl_out,
        summary_out=args.summary_out,
        delay_seconds=args.delay_seconds,
        timeout=args.timeout,
        retries=args.retries,
        refresh=args.refresh,
    )
    if args.quiet:
        print(
            json.dumps(
                {
                    "summary_out": str(args.summary_out),
                    "jsonl_out": str(args.jsonl_out),
                    "pages_ok": summary["pages_ok"],
                    "row_count": summary["row_count"],
                    "unique_download_key_count": summary["unique_download_key_count"],
                    "max_page_index_observed": summary["max_page_index_observed"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
