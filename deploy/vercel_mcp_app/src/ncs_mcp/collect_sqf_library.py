from __future__ import annotations

# DATA USE WARNING:
# This collector reads SQF library pages/files directly from ncs.go.kr.
# These posts and attachments are legacy/reference-only and are not active HRMCP
# serving evidence. Do not assume that an open-data API license covers this direct
# site collection path. Before redistribution, public serving, or snapshot
# inclusion, verify post/file-level KOGL/public-use terms and third-party rights.

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode, urljoin

import requests

from ncs_mcp.db import connect, create_indexes, initialize_database, now_utc


SQF_LIBRARY_BASE_URL = "https://www.ncs.go.kr"
SQF_LIBRARY_LIST_URL = f"{SQF_LIBRARY_BASE_URL}/sqf/sqf01/bbs_lib_list.do"
SQF_LIBRARY_DOWNLOAD_URL = f"{SQF_LIBRARY_BASE_URL}/common/file/downloadFile.do"
HTTP_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NCS-SQF-Ontology-MCP/0.1"


@dataclass
class CurlResponse:
    content: bytes
    headers: dict[str, str]
    status_code: int
    url: str
    encoding: str | None = None

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code} for {self.url}")


@dataclass(frozen=True)
class SqfLibraryFile:
    sys_dstin_cd: str
    file_mstky: str
    file_detl_seq: str
    downl_dstin_cd: str = "09"


@dataclass(frozen=True)
class SqfLibraryPost:
    lib_seq: str
    title: str
    list_page: int
    source_url: str
    published_at: str | None
    updated_at: str | None
    view_count: int | None
    source_html_hash: str
    ontology_role: str
    files: tuple[SqfLibraryFile, ...]


def content_hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def content_hash_text(value: str) -> str:
    return content_hash_bytes(value.encode("utf-8", errors="ignore"))


def content_type_encoding(content_type: str | None) -> str | None:
    if not content_type:
        return None
    match = re.search(r"charset\s*=\s*([^;\s]+)", content_type, re.I)
    return match.group(1).strip('"') if match else None


def session_cookie_jar(session: requests.Session) -> str:
    current = getattr(session, "_ncs_sqf_curl_cookie_jar", None)
    if current:
        return str(current)
    handle = tempfile.NamedTemporaryFile(prefix="ncs_sqf_", suffix=".cookies", delete=False)
    handle.close()
    path = handle.name
    setattr(session, "_ncs_sqf_curl_cookie_jar", path)
    return path


def cleanup_session_cookie_jar(session: requests.Session) -> None:
    path = getattr(session, "_ncs_sqf_curl_cookie_jar", None)
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def parse_curl_headers(raw_headers: bytes) -> tuple[int, dict[str, str]]:
    text = raw_headers.decode("iso-8859-1", errors="replace").replace("\r\n", "\n")
    blocks = [block.strip() for block in text.split("\n\n") if block.strip()]
    header_block = next((block for block in reversed(blocks) if block.startswith("HTTP/")), "")
    if not header_block:
        return 0, {}
    lines = header_block.splitlines()
    status_match = re.search(r"\s(\d{3})\s", lines[0])
    status_code = int(status_match.group(1)) if status_match else 0
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip()] = value.strip()
    return status_code, headers


def curl_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> CurlResponse:
    if params:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urlencode(params)}"
    header_file = tempfile.NamedTemporaryFile(prefix="ncs_sqf_headers_", delete=False)
    body_file = tempfile.NamedTemporaryFile(prefix="ncs_sqf_body_", delete=False)
    header_path = header_file.name
    body_path = body_file.name
    header_file.close()
    body_file.close()
    command = [
        "curl.exe",
        "-sS",
        "-L",
        "--fail",
        "--connect-timeout",
        str(timeout),
        "--max-time",
        str(max(timeout, 1)),
        "-A",
        HTTP_USER_AGENT,
        "-b",
        session_cookie_jar(session),
        "-c",
        session_cookie_jar(session),
        "-D",
        header_path,
        "-o",
        body_path,
    ]
    for key, value in (headers or {}).items():
        command.extend(["-H", f"{key}: {value}"])
    if method.upper() == "POST":
        command.extend(["-X", "POST"])
        for key, value in (data or {}).items():
            command.extend(["--data-urlencode", f"{key}={value}"])
    command.append(url)
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "curl request failed").strip()
            raise RuntimeError(message)
        header_bytes = Path(header_path).read_bytes()
        content = Path(body_path).read_bytes()
        status_code, parsed_headers = parse_curl_headers(header_bytes)
        return CurlResponse(
            content=content,
            headers=parsed_headers,
            status_code=status_code,
            url=url,
            encoding=content_type_encoding(parsed_headers.get("Content-Type")),
        )
    finally:
        for path in (header_path, body_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def get_with_fallback(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, str],
    timeout: int,
) -> requests.Response | CurlResponse:
    try:
        return session.get(url, params=params, timeout=timeout)
    except requests.RequestException:
        return curl_request(session, "GET", url, params=params, timeout=timeout)


def post_with_fallback(
    session: requests.Session,
    url: str,
    *,
    data: dict[str, str],
    timeout: int,
    headers: dict[str, str] | None = None,
) -> requests.Response | CurlResponse:
    try:
        return session.post(url, data=data, timeout=timeout, headers=headers)
    except requests.RequestException:
        return curl_request(session, "POST", url, data=data, headers=headers, timeout=timeout)


def clean_html_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", value):
        return value.replace(".", "-")
    return value or None


def infer_ontology_role(title: str) -> str:
    normalized = re.sub(r"\s+", "", title)
    if "직무역량체계도" in normalized or "직무역량체계개발" in normalized:
        return "competency_framework"
    if "훈련과정설계" in normalized:
        return "training_design"
    if "대학교육과정인정" in normalized:
        return "university_curriculum_recognition"
    if "역량인정방안" in normalized:
        return "competency_recognition"
    if "개발매뉴얼" in normalized:
        return "development_manual"
    if "활용사례" in normalized:
        return "case_study"
    if "구축방안" in normalized or "구축및활용사례" in normalized:
        return "legacy_research"
    return "reference"


def parse_download_args(value: str) -> SqfLibraryFile | None:
    quoted = re.findall(r"'([^']*)'", value)
    if len(quoted) < 3:
        return None
    downl_match = re.search(r"downlDstinCd'\s*:\s*'([^']+)'", value)
    return SqfLibraryFile(
        sys_dstin_cd=quoted[0],
        file_mstky=quoted[1],
        file_detl_seq=quoted[2],
        downl_dstin_cd=downl_match.group(1) if downl_match else "09",
    )


def fetch_library_page(
    session: requests.Session,
    page_index: int,
    *,
    timeout: int = 30,
) -> tuple[str, str]:
    params = {
        "libDstinCd": "52",
        "libSeq": "",
        "searchCondition": "",
        "searchKeyword": "",
        "pageIndex": str(page_index),
    }
    response = get_with_fallback(session, SQF_LIBRARY_LIST_URL, params=params, timeout=timeout)
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return response.text, response.url


def parse_library_posts(
    html_text: str,
    *,
    page_index: int,
    source_url: str,
) -> list[SqfLibraryPost]:
    html_hash = content_hash_text(html_text)
    posts: list[SqfLibraryPost] = []
    for row_match in re.finditer(r"<tr[^>]*>(?P<row>.*?)</tr>", html_text, re.S | re.I):
        row_html = row_match.group("row")
        view_match = re.search(
            r"fn_view\('(?P<lib_seq>[^']+)'\).*?title=\"(?P<title>[^\"]+)\"",
            row_html,
            re.S,
        )
        if not view_match:
            continue
        title = clean_html_text(view_match.group("title"))
        dates = re.findall(r"<td>\s*(\d{4}\.\d{2}\.\d{2})\s*</td>", row_html, re.S)
        numeric_cells = re.findall(r"<td>\s*([\d,]+)\s*</td>", row_html, re.S)
        views_text = numeric_cells[-1].replace(",", "") if numeric_cells else ""
        files = tuple(
            file
            for file in (
                parse_download_args(download_args)
                for download_args in re.findall(r"gfn_file_downloadFile\((.*?)\)", row_html, re.S)
            )
            if file is not None
        )
        posts.append(
            SqfLibraryPost(
                lib_seq=view_match.group("lib_seq").strip(),
                title=title,
                list_page=page_index,
                source_url=source_url,
                published_at=normalize_date(dates[0] if dates else None),
                updated_at=normalize_date(dates[1] if len(dates) > 1 else None),
                view_count=int(views_text) if views_text.isdigit() else None,
                source_html_hash=html_hash,
                ontology_role=infer_ontology_role(title),
                files=files,
            )
        )
    return posts


def _decode_header_filename(raw_value: str) -> str:
    raw_value = raw_value.strip().strip('"')
    raw_value = unquote(raw_value)
    for encoding in ("utf-8", "cp949", "euc-kr"):
        try:
            return raw_value.encode("latin1").decode(encoding)
        except UnicodeError:
            continue
    return raw_value


def filename_from_content_disposition(header_value: str | None) -> str | None:
    if not header_value:
        return None
    encoded_match = re.search(r"filename\*\s*=\s*([^;]+)", header_value, re.I)
    if encoded_match:
        value = encoded_match.group(1).strip().strip('"')
        if "''" in value:
            _, value = value.split("''", 1)
        return unquote(value)
    plain_match = re.search(r"filename\s*=\s*([^;]+)", header_value, re.I)
    if plain_match:
        return _decode_header_filename(plain_match.group(1))
    return None


def extension_from_content_type(content_type: str | None) -> str:
    content_type = (content_type or "").lower()
    if "pdf" in content_type:
        return ".pdf"
    if "hwp" in content_type or "haansoft" in content_type:
        return ".hwp"
    if "word" in content_type:
        return ".docx"
    if "zip" in content_type:
        return ".zip"
    if "excel" in content_type or "spreadsheet" in content_type:
        return ".xlsx"
    return ".bin"


def safe_filename(value: str) -> str:
    value = html.unescape(value).strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value)
    value = value.strip(" .")
    return value or "download.bin"


def upsert_library_posts(conn: sqlite3.Connection, posts: list[SqfLibraryPost]) -> dict[str, int]:
    collected_at = now_utc()
    post_count = 0
    file_count = 0
    source_count = 0
    for post in posts:
        detail_url = urljoin(SQF_LIBRARY_BASE_URL, f"/sqf/sqf01/bbs_lib_view.do?libSeq={post.lib_seq}")
        conn.execute(
            """
            INSERT INTO sqf_library_posts(
                lib_seq, title, category, list_page, detail_url, source_url,
                published_at, updated_at, view_count, source_html_hash,
                collected_at, ontology_role, extraction_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lib_seq) DO UPDATE SET
                title = excluded.title,
                list_page = excluded.list_page,
                detail_url = excluded.detail_url,
                source_url = excluded.source_url,
                published_at = excluded.published_at,
                updated_at = excluded.updated_at,
                view_count = excluded.view_count,
                source_html_hash = excluded.source_html_hash,
                collected_at = excluded.collected_at,
                ontology_role = excluded.ontology_role
            """,
            (
                post.lib_seq,
                post.title,
                "SQF 자료실",
                post.list_page,
                detail_url,
                post.source_url,
                post.published_at,
                post.updated_at,
                post.view_count,
                post.source_html_hash,
                collected_at,
                post.ontology_role,
                "metadata_collected",
            ),
        )
        post_count += 1
        if not post.files:
            continue
        for post_file in post.files:
            conn.execute(
                """
                INSERT INTO sqf_library_files(
                    lib_seq, sys_dstin_cd, file_mstky, file_detl_seq, downl_dstin_cd
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(lib_seq, sys_dstin_cd, file_mstky, file_detl_seq) DO UPDATE SET
                    downl_dstin_cd = excluded.downl_dstin_cd
                """,
                (
                    post.lib_seq,
                    post_file.sys_dstin_cd,
                    post_file.file_mstky,
                    post_file.file_detl_seq,
                    post_file.downl_dstin_cd,
                ),
            )
            file_count += 1
            file_id = conn.execute(
                """
                SELECT file_id
                FROM sqf_library_files
                WHERE lib_seq = ?
                  AND sys_dstin_cd = ?
                  AND file_mstky = ?
                  AND file_detl_seq = ?
                """,
                (
                    post.lib_seq,
                    post_file.sys_dstin_cd,
                    post_file.file_mstky,
                    post_file.file_detl_seq,
                ),
            ).fetchone()["file_id"]
            conn.execute(
                """
                INSERT INTO sqf_document_sources(
                    lib_seq, file_id, title, ontology_role, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(lib_seq, file_id) DO UPDATE SET
                    title = excluded.title,
                    ontology_role = excluded.ontology_role,
                    updated_at = ?
                """,
                (post.lib_seq, file_id, post.title, post.ontology_role, collected_at, collected_at),
            )
            source_count += 1
    conn.commit()
    return {
        "posts_upserted": post_count,
        "files_upserted": file_count,
        "document_sources_upserted": source_count,
    }


def build_download_payload(row: sqlite3.Row) -> dict[str, str]:
    return {
        "sysDstinCd": row["sys_dstin_cd"],
        "fileMstky": row["file_mstky"],
        "filedetlSeq": row["file_detl_seq"],
        "downlDstinCd": row["downl_dstin_cd"] or "09",
    }


def choose_local_filename(row: sqlite3.Row, response: requests.Response) -> str:
    filename = filename_from_content_disposition(response.headers.get("Content-Disposition"))
    if not filename:
        extension = extension_from_content_type(response.headers.get("Content-Type"))
        filename = f"{row['title']}{extension}"
    return safe_filename(f"{row['lib_seq']}_{row['file_detl_seq']}_{filename}")


def response_looks_like_error_page(response: requests.Response) -> bool:
    content_type = response.headers.get("Content-Type", "").lower()
    body_start = response.content[:300].lower()
    return "text/html" in content_type and (b"<html" in body_start or b"<script" in body_start)


def mark_file_error(conn: sqlite3.Connection, file_id: int, message: str) -> None:
    conn.execute(
        """
        UPDATE sqf_library_files
        SET download_status = 'failed',
            error_message = ?,
            downloaded_at = ?
        WHERE file_id = ?
        """,
        (message[:1000], now_utc(), file_id),
    )
    conn.commit()


def download_file(
    conn: sqlite3.Connection,
    session: requests.Session,
    row: sqlite3.Row,
    *,
    raw_dir: Path,
    timeout: int = 60,
    overwrite: bool = False,
) -> dict[str, Any]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    try:
        response = post_with_fallback(
            session,
            SQF_LIBRARY_DOWNLOAD_URL,
            data=build_download_payload(row),
            timeout=timeout,
            headers={"Referer": SQF_LIBRARY_LIST_URL},
        )
        response.raise_for_status()
        if response_looks_like_error_page(response):
            raise RuntimeError("download endpoint returned an HTML error page")
    except Exception as exc:
        mark_file_error(conn, int(row["file_id"]), str(exc))
        return {"file_id": row["file_id"], "status": "failed", "error": str(exc)}

    filename = choose_local_filename(row, response)
    local_path = raw_dir / filename
    if local_path.exists() and not overwrite:
        digest = content_hash_bytes(local_path.read_bytes())
        size = local_path.stat().st_size
    else:
        local_path.write_bytes(response.content)
        digest = content_hash_bytes(response.content)
        size = len(response.content)

    downloaded_at = now_utc()
    conn.execute(
        """
        UPDATE sqf_library_files
        SET original_filename = ?,
            content_type = ?,
            file_size = ?,
            local_path = ?,
            content_hash = ?,
            download_status = 'downloaded',
            downloaded_at = ?,
            error_message = NULL
        WHERE file_id = ?
        """,
        (
            filename,
            response.headers.get("Content-Type"),
            size,
            str(local_path),
            digest,
            downloaded_at,
            row["file_id"],
        ),
    )
    conn.execute(
        """
        UPDATE sqf_document_sources
        SET local_path = ?,
            content_hash = ?,
            updated_at = ?
        WHERE file_id = ?
        """,
        (str(local_path), digest, downloaded_at, row["file_id"]),
    )
    conn.commit()
    return {
        "file_id": row["file_id"],
        "status": "downloaded",
        "path": str(local_path),
        "bytes": size,
    }


def collect_sqf_library(
    db_path: Path,
    *,
    raw_dir: Path,
    start_page: int = 0,
    end_page: int = 10,
    download: bool = False,
    timeout: int = 30,
    overwrite: bool = False,
    delay: float = 0.2,
) -> dict[str, Any]:
    conn = connect(db_path)
    initialize_database(conn)
    create_indexes(conn)
    session = requests.Session()
    session.headers.update({"User-Agent": HTTP_USER_AGENT})
    page_summaries: list[dict[str, Any]] = []
    totals = {"posts_upserted": 0, "files_upserted": 0, "document_sources_upserted": 0}
    try:
        for page_index in range(start_page, end_page + 1):
            html_text, source_url = fetch_library_page(session, page_index, timeout=timeout)
            posts = parse_library_posts(html_text, page_index=page_index, source_url=source_url)
            upserted = upsert_library_posts(conn, posts)
            for key, value in upserted.items():
                totals[key] += value
            page_summaries.append(
                {
                    "page_index": page_index,
                    "source_url": source_url,
                    "posts_found": len(posts),
                    **upserted,
                }
            )
            if delay:
                time.sleep(delay)

        download_summary: dict[str, Any] = {"requested": False}
        if download:
            where_clause = "" if overwrite else "WHERE f.download_status != 'downloaded'"
            rows = conn.execute(
                f"""
                SELECT f.*, p.title, p.ontology_role
                FROM sqf_library_files f
                JOIN sqf_library_posts p ON p.lib_seq = f.lib_seq
                {where_clause}
                ORDER BY p.published_at DESC, f.file_id
                """
            ).fetchall()
            downloaded = 0
            failed = 0
            total_bytes = 0
            samples: list[str] = []
            for row in rows:
                result = download_file(
                    conn,
                    session,
                    row,
                    raw_dir=raw_dir,
                    timeout=timeout,
                    overwrite=overwrite,
                )
                if result["status"] == "downloaded":
                    downloaded += 1
                    total_bytes += int(result.get("bytes", 0))
                    if len(samples) < 5:
                        samples.append(result["path"])
                else:
                    failed += 1
                if delay:
                    time.sleep(delay)
            download_summary = {
                "requested": True,
                "attempted": len(rows),
                "downloaded": downloaded,
                "failed": failed,
                "total_bytes": total_bytes,
                "sample_paths": samples,
            }

        db_counts = {
            "sqf_library_posts": int(
                conn.execute("SELECT COUNT(*) FROM sqf_library_posts").fetchone()[0]
            ),
            "sqf_library_files": int(
                conn.execute("SELECT COUNT(*) FROM sqf_library_files").fetchone()[0]
            ),
            "sqf_document_sources": int(
                conn.execute("SELECT COUNT(*) FROM sqf_document_sources").fetchone()[0]
            ),
            "downloaded_files": int(
                conn.execute(
                    "SELECT COUNT(*) FROM sqf_library_files WHERE download_status = 'downloaded'"
                ).fetchone()[0]
            ),
            "failed_files": int(
                conn.execute(
                    "SELECT COUNT(*) FROM sqf_library_files WHERE download_status = 'failed'"
                ).fetchone()[0]
            ),
        }
        role_counts = {
            row["ontology_role"]: row["count"]
            for row in conn.execute(
                """
                SELECT ontology_role, COUNT(*) AS count
                FROM sqf_library_posts
                GROUP BY ontology_role
                ORDER BY count DESC
                """
            )
        }
        return {
            "source": SQF_LIBRARY_LIST_URL,
            "pages": {"start": start_page, "end": end_page},
            "raw_dir": str(raw_dir),
            "metadata": totals,
            "page_summaries": page_summaries,
            "download": download_summary,
            "counts": db_counts,
            "ontology_role_counts": role_counts,
        }
    finally:
        cleanup_session_cookie_jar(session)
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect SQF library reports for ontology sources.")
    parser.add_argument("--db", type=Path, default=Path("data/processed/ncs.db"))
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw/sqf_docs"))
    parser.add_argument("--start-page", type=int, default=0)
    parser.add_argument("--end-page", type=int, default=10)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--delay", type=float, default=0.2)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = collect_sqf_library(
        args.db,
        raw_dir=args.raw_dir,
        start_page=args.start_page,
        end_page=args.end_page,
        download=args.download,
        timeout=args.timeout,
        overwrite=args.overwrite,
        delay=args.delay,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
