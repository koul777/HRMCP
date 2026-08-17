from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_PROCESSED = PROJECT_ROOT / "data" / "processed"
REPORTS = PROJECT_ROOT / "reports"

DEFAULT_DB = DATA_PROCESSED / "ncs.db"
DEFAULT_CORPUS_AUDIT = max(
    [path for path in REPORTS.glob("sqf_corpus_audit_20*.json") if path.is_file()],
    key=lambda path: (int(next((part for part in reversed(path.stem.split("_")) if len(part) == 8 and part.isdigit()), "0")), path.stat().st_mtime),
    default=REPORTS / "sqf_corpus_audit_20260620.json",
)
DEFAULT_SAFE_OPS = max(
    [path for path in REPORTS.glob("human_review_safe_ops_checkpoint_20*.json") if path.is_file()],
    key=lambda path: (int(next((part for part in reversed(path.stem.split("_")) if len(part) == 8 and part.isdigit()), "0")), path.stat().st_mtime),
    default=REPORTS / "human_review_safe_ops_checkpoint_20260620.json",
)
DEFAULT_OUT = REPORTS / "sqf_db_readiness_checkpoint_20260620.json"
DEFAULT_MARKDOWN_OUT = REPORTS / "sqf_db_readiness_checkpoint_20260620.md"

SQF_TABLES = [
    "sqf_library_posts",
    "sqf_library_files",
    "sqf_document_sources",
    "sqf_document_assets",
    "sqf_document_pages",
    "sqf_document_chunks",
    "sqf_duties",
    "sqf_industry_sectors",
    "sqf_jobs_normalized",
    "sqf_levels",
    "sqf_job_levels_normalized",
    "sqf_recognition_evidence",
    "sqf_document_evidence_links",
    "sqf_chunk_job_level_matches",
    "sqf_ncs_matches",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return path.name


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"ok": False, "missing": True, "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": str(exc), "path": str(path)}
    return payload if isinstance(payload, dict) else {"ok": False, "error": "json_root_not_object"}


def table_counts(db_path: Path) -> dict[str, int | None]:
    if not db_path.exists():
        return {table: None for table in SQF_TABLES}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        counts: dict[str, int | None] = {}
        for table in SQF_TABLES:
            if table not in existing:
                counts[table] = None
                continue
            counts[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        return counts
    finally:
        conn.close()


def int_value(payload: dict[str, Any], key: str) -> int:
    try:
        return int(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def build_checkpoint(args: argparse.Namespace) -> dict[str, Any]:
    corpus = load_json(args.corpus_audit)
    safe_ops = load_json(args.safe_ops)
    counts = table_counts(args.db_path)
    corpus_summary = corpus.get("summary") if isinstance(corpus.get("summary"), dict) else {}
    corpus_quality = corpus.get("quality_gates") if isinstance(corpus.get("quality_gates"), dict) else {}
    safe_sqf = safe_ops.get("sqf_review") if isinstance(safe_ops.get("sqf_review"), dict) else {}
    gates = {
        "source_files_ready": (
            bool(corpus_quality.get("official_files_downloaded_and_present"))
            and int_value(corpus_summary, "official_downloaded_count") > 0
        ),
        "document_extraction_ready": (
            bool(corpus_quality.get("chunk_corpus_present"))
            and int_value(corpus_summary, "page_count") > 0
            and int_value(corpus_summary, "chunk_count") > 0
            and int_value(corpus_summary, "empty_document_count") == 0
        ),
        "normalized_sqf_model_ready": (
            (counts.get("sqf_duties") or 0) > 0
            and (counts.get("sqf_jobs_normalized") or 0) > 0
            and (counts.get("sqf_job_levels_normalized") or 0) > 0
        ),
        "matching_evidence_ready": (
            (counts.get("sqf_chunk_job_level_matches") or 0) > 0
            and (counts.get("sqf_ncs_matches") or 0) > 0
        ),
        "human_review_safe_ops_ready": (
            bool(safe_ops.get("ok"))
            and bool(safe_sqf.get("safe_for_reviewer_evidence"))
            and safe_sqf.get("allowed_use") == "supplementary_review_context_only"
        ),
        "active_scoring_blocked": (
            not bool(corpus.get("used_for_scoring"))
            and not bool(safe_sqf.get("used_for_scoring"))
            and not bool(safe_sqf.get("status_update_allowed"))
        ),
        "approval_not_auto_ready": (
            not bool(corpus.get("approval_ready"))
            and not bool(safe_sqf.get("approval_ready"))
        ),
    }
    usable = all(gates.values())
    return {
        "schema": "sqf_db_readiness_checkpoint_v1",
        "generated_at": now_iso(),
        "status": "usable_for_human_review" if usable else "review_required",
        "ok": usable,
        "allowed_use": "supplementary_review_context_only",
        "approval_ready": False,
        "used_for_scoring": False,
        "status_update_allowed": False,
        "db_writes": False,
        "sqf_table_counts": counts,
        "corpus_summary": {
            "official_file_count": int_value(corpus_summary, "official_file_count"),
            "official_downloaded_count": int_value(corpus_summary, "official_downloaded_count"),
            "document_count": int_value(corpus_summary, "document_count"),
            "page_count": int_value(corpus_summary, "page_count"),
            "chunk_count": int_value(corpus_summary, "chunk_count"),
            "chunk_match_count": int_value(corpus_summary, "chunk_match_count"),
            "sqf_ncs_candidate_count": int_value(corpus_summary, "sqf_ncs_candidate_count"),
            "empty_document_count": int_value(corpus_summary, "empty_document_count"),
        },
        "human_review_summary": {
            "claim_count": int(safe_sqf.get("claim_count") or 0),
            "p0_count": int(safe_sqf.get("p0_count") or 0),
            "pending_decision_count": int(safe_sqf.get("pending_decision_count") or 0),
            "guarded_import_candidate_count": int(safe_sqf.get("guarded_import_candidate_count") or 0),
            "planned_db_writes": int(safe_sqf.get("planned_db_writes") or 0),
        },
        "gates": gates,
        "recommended_next_actions": [
            "Use SQF report evidence as reviewer context only.",
            "Start with the SQF P0 shortlist and provenance reconfirmation sheets.",
            "Do not import status updates until explicit human decisions include reviewer id, rationale, source packet, and evidence refs.",
            "Keep active education recommendations based on NCS task/KSA/training evidence, not SQF scoring.",
        ],
        "source_artifacts": {
            "database_ref": "configured_ncs_database",
            "corpus_audit": rel(args.corpus_audit),
            "safe_ops": rel(args.safe_ops),
        },
        "policy": {
            "read_only_checkpoint": True,
            "db_writes": False,
            "status_updates": False,
            "secrets_included": False,
        },
    }


def write_markdown(path: Path, checkpoint: dict[str, Any]) -> None:
    counts = checkpoint["sqf_table_counts"]
    summary = checkpoint["corpus_summary"]
    review = checkpoint["human_review_summary"]
    lines = [
        "# SQF DB Readiness Checkpoint",
        "",
        f"- Generated at: `{checkpoint['generated_at']}`",
        f"- Status: `{checkpoint['status']}`",
        f"- OK for Human Review context: `{checkpoint['ok']}`",
        f"- Allowed use: `{checkpoint['allowed_use']}`",
        f"- Approval ready: `{checkpoint['approval_ready']}`",
        f"- Used for scoring: `{checkpoint['used_for_scoring']}`",
        f"- Status update allowed: `{checkpoint['status_update_allowed']}`",
        "",
        "## Corpus",
        "",
        f"- Official files: `{summary['official_downloaded_count']} / {summary['official_file_count']}`",
        f"- Documents: `{summary['document_count']}`",
        f"- Pages: `{summary['page_count']}`",
        f"- Chunks: `{summary['chunk_count']}`",
        f"- Chunk matches: `{summary['chunk_match_count']}`",
        f"- SQF-NCS candidates: `{summary['sqf_ncs_candidate_count']}`",
        f"- Empty documents: `{summary['empty_document_count']}`",
        "",
        "## SQF DB Tables",
        "",
        "| table | rows |",
        "|---|---:|",
    ]
    for table in SQF_TABLES:
        value = counts.get(table)
        display = "missing" if value is None else f"{value:,}"
        lines.append(f"| `{table}` | {display} |")
    lines.extend(
        [
            "",
            "## Human Review Queue",
            "",
            f"- Claims: `{review['claim_count']}`",
            f"- P0 claims: `{review['p0_count']}`",
            f"- Pending decisions: `{review['pending_decision_count']}`",
            f"- Guarded import candidates: `{review['guarded_import_candidate_count']}`",
            f"- Planned DB writes: `{review['planned_db_writes']}`",
            "",
            "## Gates",
            "",
        ]
    )
    for key, value in checkpoint["gates"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Recommended Next Actions", ""])
    for item in checkpoint["recommended_next_actions"]:
        lines.append(f"- {item}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a read-only SQF DB readiness checkpoint.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--corpus-audit", type=Path, default=DEFAULT_CORPUS_AUDIT)
    parser.add_argument("--safe-ops", type=Path, default=DEFAULT_SAFE_OPS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    args = parser.parse_args()
    checkpoint = build_checkpoint(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(args.markdown_out, checkpoint)
    print(
        json.dumps(
            {
                "out": str(args.out),
                "markdown_out": str(args.markdown_out),
                "status": checkpoint["status"],
                "ok": checkpoint["ok"],
                "gates": checkpoint["gates"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
