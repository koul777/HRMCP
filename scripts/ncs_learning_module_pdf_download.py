from __future__ import annotations

# DATA USE WARNING:
# Artifacts handled by this script come from NCS learning-module pages/files on
# ncs.go.kr. Treat them as legacy/reference-only, not active HRMCP serving data.
# Do not assume the public-data portal API record governs files downloaded from
# ncs.go.kr. Before redistribution, commercial use, or serving-snapshot inclusion,
# verify the file-level KOGL/public-use notice and any third-party rights.

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus


DOWNLOAD_URL = "https://www.ncs.go.kr/unity/hth01/hth0101/downloadFile.do"
PDF_MAGIC = b"%PDF"
SAFE_HEADER_KEYS = {
    "status_line",
    "content-type",
    "content-length",
    "content-disposition",
    "decoded_filename",
}


def _curl_binary() -> str:
    return shutil.which("curl.exe") or shutil.which("curl") or "curl.exe"


def _iter_index_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _class_code(raw: str | None) -> str:
    return (raw or "").split("_", 1)[0].strip()


def _safe_component(value: str, *, max_len: int = 80) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" ._")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:max_len] or "learning_module").strip()


def _download_stem(row: dict[str, Any]) -> str:
    code = _safe_component(_class_code(row.get("ncs_cl_cd")) or "ncs", max_len=24)
    name = _safe_component(str(row.get("competency_unit_name") or "learning_module"), max_len=80)
    key = _safe_component(str(row.get("download_key") or ""), max_len=80)
    return f"{code}_{name}_{key}"


def _headers_to_dict(headers_text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in headers_text.splitlines():
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_pdf(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < len(PDF_MAGIC):
        return False
    with path.open("rb") as handle:
        return handle.read(len(PDF_MAGIC)) == PDF_MAGIC


def _select_unique_rows(rows: list[dict[str, Any]], scope_prefix: str | None, limit: int | None) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        code = _class_code(row.get("ncs_cl_cd"))
        if scope_prefix and not code.startswith(scope_prefix):
            continue
        key = str(row.get("download_key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        selected.append(row)
        if limit is not None and len(selected) >= limit:
            break
    return selected


def _execution_scope_error(
    *,
    dry_run: bool,
    scope_prefix: str | None,
    limit: int | None,
    allow_full_mirror: bool,
) -> str | None:
    if dry_run or scope_prefix or limit is not None or allow_full_mirror:
        return None
    return "non-dry-run downloads require --scope-prefix, --limit, or explicit --allow-full-mirror"


def _curl_download(
    row: dict[str, Any],
    out_path: Path,
    raw_headers_path: Path,
    sanitized_headers_path: Path,
    *,
    curl_bin: str,
    timeout: int,
) -> dict[str, Any]:
    form = [
        ("sysDstinCd", str(row.get("sys_dstin_cd") or "")),
        ("fileMstky", str(row.get("file_mstky") or "")),
        ("filedetlSeq", str(row.get("filedetl_seq") or "")),
        ("mbrClCd", "10"),
        ("posCd", ""),
        ("downlDstinCd", "02"),
        ("ncsCompeUnitCd", ""),
        ("histYn", "N"),
    ]
    command = [
        curl_bin,
        "-L",
        "--ssl-no-revoke",
        "--http1.1",
        "--silent",
        "--show-error",
        "--max-time",
        str(timeout),
        "-D",
        str(raw_headers_path),
    ]
    for key, value in form:
        command.extend(["-F", f"{key}={value}"])
    command.extend([DOWNLOAD_URL, "-o", str(out_path), "-w", "%{http_code} %{content_type} %{size_download}"])
    started = time.time()
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    elapsed = round(time.time() - started, 3)
    status_line = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout.strip() else ""
    parts = status_line.split()
    headers = (
        _headers_to_dict(raw_headers_path.read_text(encoding="utf-8", errors="replace"))
        if raw_headers_path.exists()
        else {}
    )
    if headers:
        sanitized_headers_path.write_text(
            json.dumps(headers, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if raw_headers_path.exists():
        raw_headers_path.unlink()
    pdf_magic = _is_pdf(out_path)
    ok = proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0 and pdf_magic
    return {
        "returncode": proc.returncode,
        "http_code": parts[0] if parts else "",
        "content_type": " ".join(parts[1:-1]) if len(parts) > 2 else "",
        "size_download": parts[-1] if len(parts) > 1 else "",
        "elapsed_seconds": elapsed,
        "stderr": (proc.stderr or "").strip()[:2000],
        "headers": headers,
        "pdf_magic": pdf_magic,
        "ok": ok,
    }


def download_modules(
    *,
    index_jsonl: Path,
    out_dir: Path,
    manifest_out: Path,
    scope_prefix: str | None,
    limit: int | None,
    delay_seconds: float,
    timeout: int,
    retries: int,
    max_failures: int,
    dry_run: bool,
) -> dict[str, Any]:
    rows = _iter_index_rows(index_jsonl)
    selected = _select_unique_rows(rows, scope_prefix, limit)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    curl_bin = _curl_binary()
    downloaded = 0
    skipped_existing = 0
    failed = 0
    stopped_early = False
    stop_reason = None
    records: list[dict[str, Any]] = []
    with manifest_out.open("w", encoding="utf-8") as handle:
        for index, row in enumerate(selected, start=1):
            stem = _download_stem(row)
            out_path = out_dir / f"{stem}.pdf"
            raw_headers_path = out_dir / f"{stem}.headers.tmp"
            sanitized_headers_path = out_dir / f"{stem}.headers.json"
            base_record = {
                "index": index,
                "dry_run": dry_run,
                "download_key": row.get("download_key"),
                "ncs_cl_cd": row.get("ncs_cl_cd"),
                "competency_unit_name": row.get("competency_unit_name"),
                "learn_module_seq": row.get("learn_module_seq"),
                "out_path": str(out_path),
                "headers_path": str(sanitized_headers_path),
            }
            if dry_run:
                record = {**base_record, "status": "planned"}
            elif _is_pdf(out_path):
                skipped_existing += 1
                record = {
                    **base_record,
                    "status": "skipped_existing",
                    "bytes": out_path.stat().st_size,
                    "sha256": _sha256(out_path),
                }
            else:
                result = _curl_download(
                    row,
                    out_path,
                    raw_headers_path,
                    sanitized_headers_path,
                    curl_bin=curl_bin,
                    timeout=timeout,
                )
                retry_attempts_used = 0
                for retry_index in range(retries):
                    if result["ok"]:
                        break
                    retry_attempts_used = retry_index + 1
                    time.sleep(min(2.0 * retry_attempts_used, 10.0))
                    result = _curl_download(
                        row,
                        out_path,
                        raw_headers_path,
                        sanitized_headers_path,
                        curl_bin=curl_bin,
                        timeout=timeout,
                    )
                result["retry_attempts"] = retry_attempts_used
                if result["ok"]:
                    downloaded += 1
                    record = {
                        **base_record,
                        "status": "downloaded",
                        "bytes": out_path.stat().st_size,
                        "sha256": _sha256(out_path),
                        "download_result": result,
                    }
                else:
                    failed += 1
                    record = {**base_record, "status": "failed", "download_result": result}
            records.append(record)
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            if (
                not dry_run
                and max_failures > 0
                and failed >= max_failures
            ):
                stopped_early = True
                stop_reason = f"max_failures_reached:{max_failures}"
                break
            if delay_seconds and not dry_run and index < len(selected):
                time.sleep(delay_seconds)
    return {
        "schema": "ncs_learning_module_pdf_download_v1",
        "index_jsonl": str(index_jsonl),
        "out_dir": str(out_dir),
        "manifest_out": str(manifest_out),
        "scope_prefix": scope_prefix,
        "limit": limit,
        "dry_run": dry_run,
        "retries": retries,
        "max_failures": max_failures,
        "selected_unique_downloads": len(selected),
        "downloaded": downloaded,
        "skipped_existing": skipped_existing,
        "failed": failed,
        "stopped_early": stopped_early,
        "stop_reason": stop_reason,
        "curl_binary": curl_bin,
        "sample_records": records[:10],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index-jsonl", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--manifest-out", type=Path, required=True)
    parser.add_argument("--summary-out", type=Path, required=True)
    parser.add_argument("--scope-prefix")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--delay-seconds", type=float, default=5.0)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--max-failures", type=int, default=5)
    parser.add_argument("--allow-full-mirror", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    scope_error = _execution_scope_error(
        dry_run=args.dry_run,
        scope_prefix=args.scope_prefix,
        limit=args.limit,
        allow_full_mirror=args.allow_full_mirror,
    )
    if scope_error:
        parser.error(scope_error)
    summary = download_modules(
        index_jsonl=args.index_jsonl,
        out_dir=args.out_dir,
        manifest_out=args.manifest_out,
        scope_prefix=args.scope_prefix,
        limit=args.limit,
        delay_seconds=args.delay_seconds,
        timeout=args.timeout,
        retries=args.retries,
        max_failures=args.max_failures,
        dry_run=args.dry_run,
    )
    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    args.summary_out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
