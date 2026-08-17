from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from ncs_mcp.db import connect, create_indexes, initialize_database, now_utc


PDF_EXTENSIONS = {".pdf"}
ARCHIVE_EXTENSIONS = {".zip"}
HWP_EXTENSIONS = {".hwp"}

ONTOLOGY_TAG_PATTERNS: dict[str, list[str]] = {
    "KQF": ["KQF", "\ud55c\uad6d\ud615 \uad6d\uac00\uc5ed\ub7c9\uccb4\uacc4"],
    "SQF": ["SQF", "\uc0b0\uc5c5\ubcc4\uc5ed\ub7c9\uccb4\uacc4", "Sectoral Qualifications Framework"],
    "NCS": ["NCS", "\uad6d\uac00\uc9c1\ubb34\ub2a5\ub825\ud45c\uc900"],
    "SQF_JOB": ["SQF \uc9c1\ubb34", "\uc9c1\ubb34"],
    "SQF_LEVEL": ["SQF \uc218\uc900", "\uc218\uc900\uccb4\uacc4", "\uc218\uc900"],
    "SQF_JOB_LEVEL": ["\uc9c1\ubb34\uc218\uc900"],
    "JOB_COMPETENCY": ["\uc9c1\ubb34\uc5ed\ub7c9", "\uc9c1\ubb34\ub2a5\ub825", "\uc5ed\ub7c9"],
    "RECOGNITION_REQUIREMENT": [
        "\uc778\uc815\uae30\uc900",
        "\uc5ed\ub7c9\uc778\uc815",
        "\uc778\uc815\ubc29\uc548",
        "\uc778\uc815",
    ],
    "EDUCATION_TRAINING": [
        "\uad50\uc721\ud6c8\ub828",
        "\ud6c8\ub828\uacfc\uc815",
        "\ud6c8\ub828\ud3b8\uc131",
        "\uad50\uc721\ud504\ub85c\uadf8\ub7a8",
    ],
    "DEGREE": ["\ud559\uc704", "\ub300\ud559\uad50\uc721\uacfc\uc815", "\ud559\uad50\uad50\uc721"],
    "QUALIFICATION": ["\uc790\uaca9", "\uad6d\uac00\uae30\uc220\uc790\uaca9"],
    "CAREER": ["\uacbd\ub825", "\ud604\uc7a5\uacbd\ub825", "\uacbd\ub825\uc774\ub3d9", "\ucc44\uc6a9"],
}

KEYWORD_PATTERNS = [
    "SQF",
    "KQF",
    "NCS",
    "\uc9c1\ubb34\uc5ed\ub7c9\uccb4\uacc4",
    "\uc9c1\ubb34\uc218\uc900",
    "\uc9c1\ubb34\uc5ed\ub7c9",
    "\uad50\uc721\ud6c8\ub828",
    "\ud559\uc704",
    "\uc790\uaca9",
    "\ud604\uc7a5\uacbd\ub825",
    "\uacbd\ub825\uc774\ub3d9",
    "\ucc44\uc6a9",
    "\uc778\uc815\uae30\uc900",
    "\uc5ed\ub7c9\uc778\uc815",
    "\ud6c8\ub828\uacfc\uc815",
    "\ub300\ud559\uad50\uc721\uacfc\uc815",
]

KOREAN_STOPWORDS = {
    "\uadf8\ub9ac\uace0",
    "\ub610\ub294",
    "\uc704\ud55c",
    "\ub300\ud55c",
    "\uc788\ub2e4",
    "\ud55c\ub2e4",
    "\uac83\uc73c\ub85c",
    "\ud1b5\ud574",
    "\uc218\uc788\ub2e4",
}


def content_hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_path_part(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or "asset"


def normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n")
    value = re.sub(r"(?<=\w)-\n(?=\w)", "", value)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    paragraphs: list[str] = []
    buffer: list[str] = []
    for line in lines:
        if not line:
            if buffer:
                paragraphs.append(" ".join(buffer))
                buffer = []
            continue
        buffer.append(line)
    if buffer:
        paragraphs.append(" ".join(buffer))
    text = "\n\n".join(paragraphs)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def infer_tags(text: str) -> list[str]:
    return [
        tag
        for tag, patterns in ONTOLOGY_TAG_PATTERNS.items()
        if any(pattern in text for pattern in patterns)
    ]


def infer_keywords(text: str) -> list[str]:
    found = [keyword for keyword in KEYWORD_PATTERNS if keyword in text]
    terms = re.findall(r"[가-힣A-Za-z0-9]{2,}", text)
    counts: dict[str, int] = {}
    for term in terms:
        if term in found or term in KOREAN_STOPWORDS:
            continue
        counts[term] = counts.get(term, 0) + 1
    ranked = sorted(counts, key=lambda key: (-counts[key], key))[:20]
    return found + ranked


def chunk_pages(
    pages: list[dict[str, Any]],
    *,
    chunk_chars: int = 2400,
    overlap_chars: int = 250,
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current: list[str] = []
    page_start: int | None = None
    page_end: int | None = None
    current_len = 0

    def flush() -> None:
        nonlocal current, page_start, page_end, current_len
        text = "\n\n".join(part for part in current if part).strip()
        if text:
            chunks.append(
                {
                    "chunk_index": len(chunks),
                    "page_start": page_start,
                    "page_end": page_end,
                    "text": text,
                }
            )
        if overlap_chars and text:
            current = [text[-overlap_chars:]]
            current_len = len(current[0])
            page_start = page_end
        else:
            current = []
            current_len = 0
            page_start = None
            page_end = None

    for page in pages:
        text = page.get("text") or ""
        if not text:
            continue
        if page_start is None:
            page_start = int(page["page_no"])
        page_end = int(page["page_no"])
        parts = re.split(r"(?<=[.!?。])\s+|(?<=다\.)\s+", text)
        if len(parts) <= 1:
            parts = [text]
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if current and current_len + len(part) > chunk_chars:
                flush()
                if page_start is None:
                    page_start = int(page["page_no"])
                page_end = int(page["page_no"])
            current.append(part)
            current_len += len(part) + 2
    if current:
        flush()
    return chunks


def extract_pdf_pages_with_fitz(path: Path) -> tuple[list[dict[str, Any]], str]:
    import fitz  # type: ignore

    pages: list[dict[str, Any]] = []
    with fitz.open(path) as doc:
        for index, page in enumerate(doc, start=1):
            pages.append({"page_no": index, "text": normalize_text(page.get_text("text") or "")})
    return pages, "fitz"


def extract_pdf_pages_with_pypdf(path: Path) -> tuple[list[dict[str, Any]], str]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return (
        [
            {"page_no": index, "text": normalize_text(page.extract_text() or "")}
            for index, page in enumerate(reader.pages, start=1)
        ],
        "pypdf",
    )


def extract_pdf_pages(path: Path) -> tuple[list[dict[str, Any]], str]:
    try:
        return extract_pdf_pages_with_fitz(path)
    except Exception:
        return extract_pdf_pages_with_pypdf(path)


def hwp_control_text_to_plain(value: str) -> str:
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", value)
    value = re.sub(r"[\ue000-\uf8ff]", " ", value)
    return normalize_text(value)


def extract_hwp_pages(path: Path) -> tuple[list[dict[str, Any]], str]:
    import olefile  # type: ignore

    pages: list[dict[str, Any]] = []
    with olefile.OleFileIO(str(path)) as ole:
        header = ole.openstream("FileHeader").read()
        compressed = len(header) >= 40 and bool(int.from_bytes(header[36:40], "little") & 1)
        section_paths = [
            "/".join(parts)
            for parts in ole.listdir()
            if len(parts) == 2
            and parts[0] == "BodyText"
            and re.fullmatch(r"Section\d+", parts[1])
        ]
        section_paths.sort(key=lambda item: int(re.search(r"\d+", item).group(0)))  # type: ignore[union-attr]
        for index, section_path in enumerate(section_paths, start=1):
            data = ole.openstream(section_path).read()
            if compressed:
                import zlib

                try:
                    data = zlib.decompress(data, -15)
                except zlib.error:
                    data = zlib.decompress(data)
            text = hwp_control_text_to_plain(data.decode("utf-16le", errors="ignore"))
            pages.append({"page_no": index, "text": text})
    return pages, "hwp-olefile"


def tesseract_ocr_available() -> tuple[bool, str | None]:
    try:
        import pytesseract  # type: ignore

        local_tessdata = Path("data/ocr/tessdata")
        if local_tessdata.exists():
            os.environ.setdefault("TESSDATA_PREFIX", str(local_tessdata.resolve()))
        command = shutil.which("tesseract")
        if command is None:
            for candidate in [
                Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
                Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
            ]:
                if candidate.exists():
                    command = str(candidate)
                    break
        if command:
            pytesseract.pytesseract.tesseract_cmd = command
        version = pytesseract.get_tesseract_version()
        return True, str(version)
    except Exception as exc:
        return False, str(exc)


def extract_pdf_pages_with_tesseract(
    path: Path,
    *,
    lang: str = "kor+eng",
    dpi: int = 180,
    max_pages: int | None = None,
) -> tuple[list[dict[str, Any]], str]:
    import fitz  # type: ignore
    import pytesseract  # type: ignore
    from PIL import Image

    local_tessdata = Path("data/ocr/tessdata")
    if local_tessdata.exists():
        os.environ.setdefault("TESSDATA_PREFIX", str(local_tessdata.resolve()))
    command = shutil.which("tesseract")
    if command is None:
        for candidate in [
            Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
            Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
        ]:
            if candidate.exists():
                command = str(candidate)
                break
    if command:
        pytesseract.pytesseract.tesseract_cmd = command

    pages: list[dict[str, Any]] = []
    with fitz.open(path) as doc:
        for index, page in enumerate(doc, start=1):
            if max_pages is not None and index > max_pages:
                break
            matrix = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            text = pytesseract.image_to_string(image, lang=lang)
            pages.append({"page_no": index, "text": normalize_text(text or "")})
    return pages, f"tesseract-ocr:{lang}:{dpi}dpi"


def upsert_asset(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    asset_path: Path,
    parent_archive_path: Path | None = None,
    asset_type: str | None = None,
) -> int:
    timestamp = now_utc()
    content = asset_path.read_bytes()
    conn.execute(
        """
        INSERT INTO sqf_document_assets(
            document_id, asset_path, parent_archive_path, asset_name,
            asset_type, content_hash, file_size, extraction_status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        ON CONFLICT(asset_path) DO UPDATE SET
            document_id = excluded.document_id,
            parent_archive_path = excluded.parent_archive_path,
            asset_name = excluded.asset_name,
            asset_type = excluded.asset_type,
            content_hash = excluded.content_hash,
            file_size = excluded.file_size,
            updated_at = excluded.updated_at
        """,
        (
            document_id,
            str(asset_path),
            str(parent_archive_path) if parent_archive_path else None,
            asset_path.name,
            asset_type or asset_path.suffix.lower().lstrip(".") or "unknown",
            content_hash_bytes(content),
            len(content),
            timestamp,
            timestamp,
        ),
    )
    return int(
        conn.execute(
            "SELECT asset_id FROM sqf_document_assets WHERE asset_path = ?",
            (str(asset_path),),
        ).fetchone()["asset_id"]
    )


def clear_asset_text(conn: sqlite3.Connection, asset_id: int) -> None:
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
    conn.execute("DELETE FROM sqf_document_chunks WHERE asset_id = ?", (asset_id,))
    conn.execute("DELETE FROM sqf_document_pages WHERE asset_id = ?", (asset_id,))


def mark_asset_status(
    conn: sqlite3.Connection,
    asset_id: int,
    *,
    status: str,
    engine: str | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE sqf_document_assets
        SET extraction_status = ?,
            extraction_engine = ?,
            error_message = ?,
            updated_at = ?
        WHERE asset_id = ?
        """,
        (status, engine, error[:1000] if error else None, now_utc(), asset_id),
    )


def store_pdf_text(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    path: Path,
    chunk_chars: int,
    overlap_chars: int,
    ocr_empty: bool = False,
    ocr_lang: str = "kor+eng",
    ocr_dpi: int = 180,
    ocr_max_pages: int | None = None,
) -> dict[str, int | str]:
    clear_asset_text(conn, asset_id)
    try:
        pages, engine = extract_pdf_pages(path)
    except Exception as exc:
        mark_asset_status(conn, asset_id, status="failed", error=str(exc))
        return {"status": "failed", "pages": 0, "chunks": 0, "chars": 0}

    timestamp = now_utc()
    ocr_unavailable_error: str | None = None
    total_chars = sum(len(page["text"]) for page in pages)
    if total_chars == 0 and ocr_empty:
        available, detail = tesseract_ocr_available()
        if available:
            try:
                pages, engine = extract_pdf_pages_with_tesseract(
                    path,
                    lang=ocr_lang,
                    dpi=ocr_dpi,
                    max_pages=ocr_max_pages,
                )
                total_chars = sum(len(page["text"]) for page in pages)
            except Exception as exc:
                mark_asset_status(
                    conn,
                    asset_id,
                    status="failed",
                    engine="tesseract-ocr",
                    error=str(exc),
                )
                return {"status": "failed", "pages": 0, "chunks": 0, "chars": 0}
        else:
            ocr_unavailable_error = f"OCR requested but Tesseract is unavailable: {detail}"

    for page in pages:
        text = page["text"]
        conn.execute(
            """
            INSERT INTO sqf_document_pages(
                asset_id, page_no, text, char_count, extraction_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id, page_no) DO UPDATE SET
                text = excluded.text,
                char_count = excluded.char_count,
                extraction_status = excluded.extraction_status
            """,
            (
                asset_id,
                page["page_no"],
                text,
                len(text),
                "empty" if not text else "extracted",
                timestamp,
            ),
        )

    chunks = chunk_pages(pages, chunk_chars=chunk_chars, overlap_chars=overlap_chars)
    for chunk in chunks:
        text = chunk["text"]
        conn.execute(
            """
            INSERT INTO sqf_document_chunks(
                asset_id, chunk_index, page_start, page_end, text,
                char_count, token_estimate, keywords_json, ontology_tags_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id, chunk_index) DO UPDATE SET
                page_start = excluded.page_start,
                page_end = excluded.page_end,
                text = excluded.text,
                char_count = excluded.char_count,
                token_estimate = excluded.token_estimate,
                keywords_json = excluded.keywords_json,
                ontology_tags_json = excluded.ontology_tags_json
            """,
            (
                asset_id,
                chunk["chunk_index"],
                chunk["page_start"],
                chunk["page_end"],
                text,
                len(text),
                max(1, len(text) // 3),
                json.dumps(infer_keywords(text), ensure_ascii=False),
                json.dumps(infer_tags(text), ensure_ascii=False),
                timestamp,
            ),
        )

    status = "extracted" if total_chars else "empty"
    mark_asset_status(conn, asset_id, status=status, engine=engine, error=ocr_unavailable_error)
    return {"status": status, "pages": len(pages), "chunks": len(chunks), "chars": total_chars}


def store_hwp_text(
    conn: sqlite3.Connection,
    *,
    asset_id: int,
    path: Path,
    chunk_chars: int,
    overlap_chars: int,
) -> dict[str, int | str]:
    clear_asset_text(conn, asset_id)
    try:
        pages, engine = extract_hwp_pages(path)
    except Exception as exc:
        mark_asset_status(conn, asset_id, status="failed", engine="hwp-olefile", error=str(exc))
        return {"status": "failed", "pages": 0, "chunks": 0, "chars": 0}

    timestamp = now_utc()
    total_chars = 0
    for page in pages:
        text = page["text"]
        total_chars += len(text)
        conn.execute(
            """
            INSERT INTO sqf_document_pages(
                asset_id, page_no, text, char_count, extraction_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id, page_no) DO UPDATE SET
                text = excluded.text,
                char_count = excluded.char_count,
                extraction_status = excluded.extraction_status
            """,
            (
                asset_id,
                page["page_no"],
                text,
                len(text),
                "empty" if not text else "extracted",
                timestamp,
            ),
        )

    chunks = chunk_pages(pages, chunk_chars=chunk_chars, overlap_chars=overlap_chars)
    for chunk in chunks:
        text = chunk["text"]
        conn.execute(
            """
            INSERT INTO sqf_document_chunks(
                asset_id, chunk_index, page_start, page_end, text,
                char_count, token_estimate, keywords_json, ontology_tags_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id, chunk_index) DO UPDATE SET
                page_start = excluded.page_start,
                page_end = excluded.page_end,
                text = excluded.text,
                char_count = excluded.char_count,
                token_estimate = excluded.token_estimate,
                keywords_json = excluded.keywords_json,
                ontology_tags_json = excluded.ontology_tags_json
            """,
            (
                asset_id,
                chunk["chunk_index"],
                chunk["page_start"],
                chunk["page_end"],
                text,
                len(text),
                max(1, len(text) // 3),
                json.dumps(infer_keywords(text), ensure_ascii=False),
                json.dumps(infer_tags(text), ensure_ascii=False),
                timestamp,
            ),
        )

    status = "extracted" if total_chars else "empty"
    mark_asset_status(conn, asset_id, status=status, engine=engine)
    return {"status": status, "pages": len(pages), "chunks": len(chunks), "chars": total_chars}


def safe_extract_zip(zip_path: Path, out_dir: Path) -> list[Path]:
    extracted: list[Path] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename.replace("\\", "/")).name
            if not name:
                continue
            target = out_dir / safe_path_part(name)
            with archive.open(info) as source:
                target.write_bytes(source.read())
            extracted.append(target)
    return extracted


def iter_document_file_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            ds.document_id, ds.title, ds.ontology_role,
            f.file_id, f.local_path, f.original_filename, f.download_status
        FROM sqf_document_sources ds
        JOIN sqf_library_files f ON f.file_id = ds.file_id
        WHERE f.local_path IS NOT NULL
          AND f.download_status = 'downloaded'
        ORDER BY ds.document_id
        """
    ).fetchall()


def has_unprocessed_assets(conn: sqlite3.Connection, document_id: int) -> bool:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total_assets,
            SUM(CASE WHEN extraction_status != 'extracted' THEN 1 ELSE 0 END) AS pending_assets
        FROM sqf_document_assets
        WHERE document_id = ?
        """,
        (document_id,),
    ).fetchone()
    total_assets = int(row["total_assets"] or 0)
    pending_assets = int(row["pending_assets"] or 0)
    return total_assets == 0 or pending_assets > 0


def update_document_status(conn: sqlite3.Connection, document_id: int) -> None:
    rows = conn.execute(
        """
        SELECT extraction_status, COUNT(*) AS count
        FROM sqf_document_assets
        WHERE document_id = ?
        GROUP BY extraction_status
        """,
        (document_id,),
    ).fetchall()
    counts = {row["extraction_status"]: row["count"] for row in rows}
    if counts.get("extracted"):
        status = "extracted"
    elif counts.get("empty") and not counts.get("failed"):
        status = "empty"
    elif counts.get("failed"):
        status = "failed"
    elif counts.get("unsupported"):
        status = "unsupported"
    else:
        status = "pending"
    conn.execute(
        """
        UPDATE sqf_document_sources
        SET text_extraction_status = ?,
            updated_at = ?
        WHERE document_id = ?
        """,
        (status, now_utc(), document_id),
    )


def preprocess_sqf_documents(
    db_path: Path,
    *,
    extracted_dir: Path = Path("data/raw/sqf_docs_extracted"),
    chunk_chars: int = 2400,
    overlap_chars: int = 250,
    limit: int | None = None,
    ocr_empty: bool = False,
    ocr_lang: str = "kor+eng",
    ocr_dpi: int = 180,
    ocr_max_pages: int | None = None,
    only_unprocessed: bool = False,
) -> dict[str, Any]:
    conn = connect(db_path)
    initialize_database(conn)
    create_indexes(conn)
    stats: dict[str, Any] = {
        "documents_seen": 0,
        "assets_seen": 0,
        "pdf_assets": 0,
        "hwp_assets": 0,
        "archives": 0,
        "unsupported_assets": 0,
        "failed_assets": 0,
        "pages": 0,
        "chunks": 0,
        "chars": 0,
        "ocr_requested": ocr_empty,
    }
    try:
        rows = iter_document_file_rows(conn)
        if limit is not None:
            rows = rows[:limit]
        for row in rows:
            if only_unprocessed and not has_unprocessed_assets(conn, int(row["document_id"])):
                continue
            stats["documents_seen"] += 1
            local_path = Path(row["local_path"])
            suffix = local_path.suffix.lower()
            asset_paths: list[tuple[Path, Path | None]] = []
            if suffix in ARCHIVE_EXTENSIONS:
                stats["archives"] += 1
                archive_out = extracted_dir / f"{row['file_id']}_{safe_path_part(local_path.stem)}"
                for extracted in safe_extract_zip(local_path, archive_out):
                    asset_paths.append((extracted, local_path))
            else:
                asset_paths.append((local_path, None))

            for asset_path, parent_archive in asset_paths:
                stats["assets_seen"] += 1
                asset_id = upsert_asset(
                    conn,
                    document_id=int(row["document_id"]),
                    asset_path=asset_path,
                    parent_archive_path=parent_archive,
                )
                if asset_path.suffix.lower() in PDF_EXTENSIONS:
                    stats["pdf_assets"] += 1
                    result = store_pdf_text(
                        conn,
                        asset_id=asset_id,
                        path=asset_path,
                        chunk_chars=chunk_chars,
                        overlap_chars=overlap_chars,
                        ocr_empty=ocr_empty,
                        ocr_lang=ocr_lang,
                        ocr_dpi=ocr_dpi,
                        ocr_max_pages=ocr_max_pages,
                    )
                    if result["status"] == "failed":
                        stats["failed_assets"] += 1
                    stats["pages"] += int(result["pages"])
                    stats["chunks"] += int(result["chunks"])
                    stats["chars"] += int(result["chars"])
                elif asset_path.suffix.lower() in HWP_EXTENSIONS:
                    stats["hwp_assets"] += 1
                    result = store_hwp_text(
                        conn,
                        asset_id=asset_id,
                        path=asset_path,
                        chunk_chars=chunk_chars,
                        overlap_chars=overlap_chars,
                    )
                    if result["status"] == "failed":
                        stats["failed_assets"] += 1
                    stats["pages"] += int(result["pages"])
                    stats["chunks"] += int(result["chunks"])
                    stats["chars"] += int(result["chars"])
                else:
                    clear_asset_text(conn, asset_id)
                    mark_asset_status(
                        conn,
                        asset_id,
                        status="unsupported",
                        error=f"Unsupported extension: {asset_path.suffix}",
                    )
                    stats["unsupported_assets"] += 1
            update_document_status(conn, int(row["document_id"]))
            conn.commit()

        stats["db_counts"] = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in [
                "sqf_document_sources",
                "sqf_document_assets",
                "sqf_document_pages",
                "sqf_document_chunks",
            ]
        }
        stats["status_counts"] = {
            row["extraction_status"]: row["count"]
            for row in conn.execute(
                """
                SELECT extraction_status, COUNT(*) AS count
                FROM sqf_document_assets
                GROUP BY extraction_status
                ORDER BY extraction_status
                """
            )
        }
        return stats
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract SQF PDF report text into SQLite.")
    parser.add_argument("--db", type=Path, default=Path("data/processed/ncs.db"))
    parser.add_argument("--extracted-dir", type=Path, default=Path("data/raw/sqf_docs_extracted"))
    parser.add_argument("--chunk-chars", type=int, default=2400)
    parser.add_argument("--overlap-chars", type=int, default=250)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ocr-empty", action="store_true")
    parser.add_argument("--ocr-lang", default="kor+eng")
    parser.add_argument("--ocr-dpi", type=int, default=180)
    parser.add_argument("--ocr-max-pages", type=int)
    parser.add_argument("--only-unprocessed", action="store_true")
    args = parser.parse_args()
    result = preprocess_sqf_documents(
        args.db,
        extracted_dir=args.extracted_dir,
        chunk_chars=args.chunk_chars,
        overlap_chars=args.overlap_chars,
        limit=args.limit,
        ocr_empty=args.ocr_empty,
        ocr_lang=args.ocr_lang,
        ocr_dpi=args.ocr_dpi,
        ocr_max_pages=args.ocr_max_pages,
        only_unprocessed=args.only_unprocessed,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
