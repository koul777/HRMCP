from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import re
import shutil
from pathlib import Path
from typing import Any

from ncs_mcp.db import connect, create_indexes, initialize_database, now_utc


def content_hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or "ontology_source"


def stable_local_lib_seq(path: Path, digest: str) -> str:
    return f"local-{digest[:16]}-{safe_filename(path.stem)[:48]}"


def register_local_ontology_source(
    db_path: Path,
    source_path: Path,
    *,
    raw_dir: Path = Path("data/raw/ontology_sources"),
    title: str | None = None,
    ontology_role: str = "framework_reference",
    source_url: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    source_path = source_path.expanduser().resolve()
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f"Ontology source file does not exist: {source_path}")

    content = source_path.read_bytes()
    digest = content_hash_bytes(content)
    lib_seq = stable_local_lib_seq(source_path, digest)
    filename = safe_filename(source_path.name)
    target_dir = raw_dir.expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{digest[:16]}_{filename}"
    if not target_path.exists() or content_hash_bytes(target_path.read_bytes()) != digest:
        shutil.copy2(source_path, target_path)

    content_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
    timestamp = now_utc()
    document_title = title or source_path.stem
    source_url = source_url or f"local:{source_path}"
    file_mstky = digest[:32]
    file_detl_seq = "1"

    conn = connect(db_path)
    initialize_database(conn)
    create_indexes(conn)
    try:
        conn.execute(
            """
            INSERT INTO sqf_library_posts(
                lib_seq, title, category, list_page, detail_url, source_url,
                published_at, updated_at, view_count, source_html_hash,
                collected_at, ontology_role, extraction_status, notes
            ) VALUES (?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?)
            ON CONFLICT(lib_seq) DO UPDATE SET
                title = excluded.title,
                category = excluded.category,
                source_url = excluded.source_url,
                source_html_hash = excluded.source_html_hash,
                collected_at = excluded.collected_at,
                ontology_role = excluded.ontology_role,
                extraction_status = excluded.extraction_status,
                notes = excluded.notes
            """,
            (
                lib_seq,
                document_title,
                "local_ontology_source",
                source_url,
                digest,
                timestamp,
                ontology_role,
                "metadata_collected",
                notes,
            ),
        )
        conn.execute(
            """
            INSERT INTO sqf_library_files(
                lib_seq, sys_dstin_cd, file_mstky, file_detl_seq, downl_dstin_cd,
                original_filename, content_type, file_size, local_path, content_hash,
                download_status, downloaded_at, error_message
            ) VALUES (?, 'LOCAL', ?, ?, 'local', ?, ?, ?, ?, ?, 'downloaded', ?, NULL)
            ON CONFLICT(lib_seq, sys_dstin_cd, file_mstky, file_detl_seq) DO UPDATE SET
                original_filename = excluded.original_filename,
                content_type = excluded.content_type,
                file_size = excluded.file_size,
                local_path = excluded.local_path,
                content_hash = excluded.content_hash,
                download_status = excluded.download_status,
                downloaded_at = excluded.downloaded_at,
                error_message = NULL
            """,
            (
                lib_seq,
                file_mstky,
                file_detl_seq,
                source_path.name,
                content_type,
                len(content),
                str(target_path),
                digest,
                timestamp,
            ),
        )
        file_id = int(
            conn.execute(
                """
                SELECT file_id
                FROM sqf_library_files
                WHERE lib_seq = ?
                  AND sys_dstin_cd = 'LOCAL'
                  AND file_mstky = ?
                  AND file_detl_seq = ?
                """,
                (lib_seq, file_mstky, file_detl_seq),
            ).fetchone()["file_id"]
        )
        conn.execute(
            """
            INSERT INTO sqf_document_sources(
                lib_seq, file_id, title, ontology_role, local_path, content_hash,
                text_extraction_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(lib_seq, file_id) DO UPDATE SET
                title = excluded.title,
                ontology_role = excluded.ontology_role,
                local_path = excluded.local_path,
                content_hash = excluded.content_hash,
                text_extraction_status =
                    CASE
                        WHEN sqf_document_sources.content_hash = excluded.content_hash
                         AND sqf_document_sources.text_extraction_status = 'extracted'
                        THEN sqf_document_sources.text_extraction_status
                        ELSE 'pending'
                    END,
                updated_at = excluded.updated_at
            """,
            (
                lib_seq,
                file_id,
                document_title,
                ontology_role,
                str(target_path),
                digest,
                timestamp,
                timestamp,
            ),
        )
        document_id = int(
            conn.execute(
                """
                SELECT document_id
                FROM sqf_document_sources
                WHERE lib_seq = ? AND file_id = ?
                """,
                (lib_seq, file_id),
            ).fetchone()["document_id"]
        )
        conn.commit()
        return {
            "document_id": document_id,
            "file_id": file_id,
            "lib_seq": lib_seq,
            "title": document_title,
            "ontology_role": ontology_role,
            "source_path": str(source_path),
            "stored_path": str(target_path),
            "content_hash": digest,
            "file_size": len(content),
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Register local ontology source documents.")
    parser.add_argument("--db", type=Path, default=Path("data/processed/ncs.db"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw/ontology_sources"))
    parser.add_argument("--title")
    parser.add_argument("--role", default="framework_reference")
    parser.add_argument("--source-url")
    parser.add_argument("--notes")
    args = parser.parse_args()
    result = register_local_ontology_source(
        args.db,
        args.input,
        raw_dir=args.raw_dir,
        title=args.title,
        ontology_role=args.role,
        source_url=args.source_url,
        notes=args.notes,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
