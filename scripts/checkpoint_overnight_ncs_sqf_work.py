from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS = PROJECT_ROOT / "reports"

DEFAULT_NCS006 = REPORTS / "checkpoint_ncs006_element_api_status_20260624_current.json"
DEFAULT_SQF_DB = max(
    [path for path in REPORTS.glob("sqf_db_readiness_checkpoint_20*.json") if path.is_file()],
    key=lambda path: (int(next((part for part in reversed(path.stem.split("_")) if len(part) == 8 and part.isdigit()), "0")), path.stat().st_mtime),
    default=REPORTS / "sqf_db_readiness_checkpoint_20260620.json",
)
DEFAULT_HUMAN_REVIEW = max(
    [path for path in REPORTS.glob("human_review_safe_ops_checkpoint_20*.json") if path.is_file()],
    key=lambda path: (int(next((part for part in reversed(path.stem.split("_")) if len(part) == 8 and part.isdigit()), "0")), path.stat().st_mtime),
    default=REPORTS / "human_review_safe_ops_checkpoint_20260620.json",
)
DEFAULT_OUT = REPORTS / "overnight_ncs_sqf_work_checkpoint_20260620.json"
DEFAULT_MARKDOWN_OUT = REPORTS / "overnight_ncs_sqf_work_checkpoint_20260620.md"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"ok": False, "missing": True, "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": str(exc), "path": str(path)}
    return payload if isinstance(payload, dict) else {"ok": False, "error": "json_root_not_object"}


def summarize_ncs006(payload: dict[str, Any]) -> dict[str, Any]:
    status = payload.get("element_api_status") if isinstance(payload.get("element_api_status"), dict) else {}
    totals = status.get("totals") if isinstance(status.get("totals"), dict) else {}
    by_major = status.get("by_major") if isinstance(status.get("by_major"), list) else []
    active = (
        payload.get("run_log", {}).get("active_or_incomplete_batch")
        if isinstance(payload.get("run_log"), dict)
        else {}
    ) or {}
    monitoring = payload.get("monitoring") if isinstance(payload.get("monitoring"), dict) else {}
    cooldown = (
        payload.get("rate_limit_cooldown")
        if isinstance(payload.get("rate_limit_cooldown"), dict)
        else {}
    )
    forecast = payload.get("throughput_forecast") if isinstance(payload.get("throughput_forecast"), dict) else {}
    active_code = active.get("major_code") or monitoring.get("active_major_code")
    active_major = next(
        (
            item
            for item in by_major
            if isinstance(item, dict) and str(item.get("major_code")) == str(active_code)
        ),
        {},
    )
    return {
        "ok": bool(payload) and not bool(payload.get("missing")) and not bool(monitoring.get("timeout_exceeded")),
        "matched": int(totals.get("matched") or 0),
        "total": int(totals.get("total") or 0),
        "matched_ratio": float(totals.get("matched_ratio") or 0.0),
        "not_collected": int(totals.get("not_collected") or 0),
        "api_failed": int(totals.get("api_failed") or 0),
        "active_major_code": active_code,
        "active_major_name": active_major.get("major_name_display") or active_major.get("major_name"),
        "active_phase": active.get("phase") or monitoring.get("active_phase"),
        "active_batch_monitor_status": monitoring.get("status"),
        "active_batch_age_seconds": monitoring.get("active_batch_age_seconds"),
        "rate_limit_cooldown_status": cooldown.get("status"),
        "rate_limit_cooldown_until": cooldown.get("cooldown_until"),
        "rate_limit_cooldown_remaining_seconds": cooldown.get("cooldown_remaining_seconds"),
        "process_count": len(payload.get("collection_processes") or []),
        "completed_batches": (
            payload.get("run_log", {}).get("completed_batches_since_latest_start")
            if isinstance(payload.get("run_log"), dict)
            else None
        ),
        "rate_limited": (
            payload.get("run_log", {}).get("rate_limited_since_latest_start")
            if isinstance(payload.get("run_log"), dict)
            else None
        ),
        "estimated_remaining_hours": forecast.get("estimated_remaining_hours"),
        "top_remaining_majors": [
            {
                "major_code": item.get("major_code"),
                "major_name": item.get("major_name_display") or item.get("major_name"),
                "not_collected": int(item.get("not_collected") or 0),
                "api_failed": int(item.get("api_failed") or 0),
                "matched": int(item.get("matched") or 0),
                "total": int(item.get("total") or 0),
            }
            for item in sorted(
                [item for item in by_major if isinstance(item, dict)],
                key=lambda row: int(row.get("not_collected") or 0) + int(row.get("api_failed") or 0),
                reverse=True,
            )[:8]
        ],
    }


def summarize_sqf_db(payload: dict[str, Any]) -> dict[str, Any]:
    corpus = payload.get("corpus_summary") if isinstance(payload.get("corpus_summary"), dict) else {}
    review = payload.get("human_review_summary") if isinstance(payload.get("human_review_summary"), dict) else {}
    return {
        "ok": bool(payload.get("ok")),
        "status": payload.get("status"),
        "allowed_use": payload.get("allowed_use"),
        "approval_ready": bool(payload.get("approval_ready")),
        "used_for_scoring": bool(payload.get("used_for_scoring")),
        "status_update_allowed": bool(payload.get("status_update_allowed")),
        "official_downloaded_count": int(corpus.get("official_downloaded_count") or 0),
        "official_file_count": int(corpus.get("official_file_count") or 0),
        "page_count": int(corpus.get("page_count") or 0),
        "chunk_count": int(corpus.get("chunk_count") or 0),
        "sqf_ncs_candidate_count": int(corpus.get("sqf_ncs_candidate_count") or 0),
        "claim_count": int(review.get("claim_count") or 0),
        "p0_count": int(review.get("p0_count") or 0),
        "pending_decision_count": int(review.get("pending_decision_count") or 0),
        "guarded_import_candidate_count": int(review.get("guarded_import_candidate_count") or 0),
    }


def summarize_human_review(payload: dict[str, Any]) -> dict[str, Any]:
    sqf = payload.get("sqf_review") if isinstance(payload.get("sqf_review"), dict) else {}
    provenance = (
        payload.get("legacy_trusted_status_provenance")
        if isinstance(payload.get("legacy_trusted_status_provenance"), dict)
        else {}
    )
    decision_sheet = (
        payload.get("legacy_trusted_status_reconfirmation_decision_sheet")
        if isinstance(payload.get("legacy_trusted_status_reconfirmation_decision_sheet"), dict)
        else {}
    )
    rows_without_packet_backed = int(
        provenance.get("rows_without_packet_backed_provenance") or 0
    )
    reconfirmation_blank_decision_count = int(decision_sheet.get("blank_decision_count") or 0)
    unresolved_provenance_gap = (
        bool(provenance.get("provenance_gap_present"))
        or rows_without_packet_backed > 0
        or reconfirmation_blank_decision_count > 0
    )
    return {
        "ok": bool(payload.get("ok")),
        "sqf_safe_for_reviewer_evidence": bool(sqf.get("safe_for_reviewer_evidence")),
        "sqf_pending_decision_count": int(sqf.get("pending_decision_count") or 0),
        "sqf_guarded_import_candidate_count": int(sqf.get("guarded_import_candidate_count") or 0),
        "sqf_planned_db_writes": int(sqf.get("planned_db_writes") or 0),
        "legacy_trusted_status_rows_pending_reconfirmation": int(
            provenance.get("legacy_trusted_status_rows_pending_reconfirmation")
            or provenance.get("trusted_row_count")
            or 0
        ),
        "rows_without_packet_backed_provenance": rows_without_packet_backed,
        "provenance_gap_present": bool(provenance.get("provenance_gap_present")),
        "reconfirmation_blank_decision_count": reconfirmation_blank_decision_count,
        "reconfirmation_completed_decision_count": int(decision_sheet.get("completed_decision_count") or 0),
        "unresolved_provenance_gap": unresolved_provenance_gap,
        "reviewer_safe_artifacts": payload.get("reviewer_safe_artifacts") or [],
    }


def build_checkpoint(args: argparse.Namespace) -> dict[str, Any]:
    ncs006_raw = load_json(args.ncs006)
    sqf_db_raw = load_json(args.sqf_db)
    human_review_raw = load_json(args.human_review)
    ncs006 = summarize_ncs006(ncs006_raw)
    sqf_db = summarize_sqf_db(sqf_db_raw)
    human_review = summarize_human_review(human_review_raw)
    ok = (
        ncs006["ok"]
        and sqf_db["ok"]
        and human_review["ok"]
        and not sqf_db["approval_ready"]
        and not sqf_db["used_for_scoring"]
        and not sqf_db["status_update_allowed"]
        and human_review["sqf_planned_db_writes"] == 0
        and not human_review["unresolved_provenance_gap"]
    )
    review_gate_code = None
    if human_review["unresolved_provenance_gap"]:
        review_gate_code = "human_review_provenance_reconfirmation_required"
    elif not sqf_db["ok"]:
        review_gate_code = "sqf_human_review_required"
    return {
        "schema": "overnight_ncs_sqf_work_checkpoint_v1",
        "generated_at": now_iso(),
        "ok": ok,
        "contract_ok": ok,
        "review_gated": bool(review_gate_code),
        "review_gate_code": review_gate_code,
        "ncs006_collection": ncs006,
        "sqf_db_readiness": sqf_db,
        "human_review_safe_ops": human_review,
        "operator_notes": [
            "NCS006 element API collection remains the active DB-writing background job; do not start a duplicate collector.",
            "SQF DB evidence is usable for Human Review context, not automatic approval or active recommendation scoring.",
            "Review provenance reconfirmation artifacts before treating legacy trusted rows as reliable.",
            "Distribute reviewer-facing md/html/csv artifacts first; keep JSON artifacts for internal audit.",
        ],
        "source_artifacts": {
            "ncs006": rel(args.ncs006),
            "sqf_db": rel(args.sqf_db),
            "human_review": rel(args.human_review),
        },
        "policy": {
            "read_only_checkpoint": True,
            "db_writes": False,
            "status_updates": False,
            "secrets_included": False,
        },
    }


def write_markdown(path: Path, checkpoint: dict[str, Any]) -> None:
    ncs = checkpoint["ncs006_collection"]
    sqf = checkpoint["sqf_db_readiness"]
    review = checkpoint["human_review_safe_ops"]
    lines = [
        "# Overnight NCS/SQF Work Checkpoint",
        "",
        f"- Generated at: `{checkpoint['generated_at']}`",
        f"- Overall ok: `{checkpoint['ok']}`",
        "",
        "## NCS006 Collection",
        "",
        f"- Matched: `{ncs['matched']:,} / {ncs['total']:,}` ({ncs['matched_ratio']:.2%})",
        f"- Remaining not_collected: `{ncs['not_collected']:,}`",
        f"- API failed: `{ncs['api_failed']:,}`",
        f"- Active major: `{ncs['active_major_code']} {ncs['active_major_name']}`",
        f"- Active monitor status: `{ncs['active_batch_monitor_status']}`",
        f"- Active batch age seconds: `{ncs['active_batch_age_seconds']}`",
        f"- Rate-limit cooldown status: `{ncs['rate_limit_cooldown_status']}`",
        f"- Cooldown until: `{ncs['rate_limit_cooldown_until']}`",
        f"- Cooldown remaining seconds: `{ncs['rate_limit_cooldown_remaining_seconds']}`",
        f"- Collection process count: `{ncs['process_count']}`",
        f"- Completed batches: `{ncs['completed_batches']}`",
        f"- Rate limited: `{ncs['rate_limited']}`",
        f"- Estimated remaining hours: `{ncs['estimated_remaining_hours']}`",
        "",
        "### Largest Remaining Majors",
        "",
        "| Major | Not Collected | API Failed | Matched | Total |",
        "|---|---:|---:|---:|---:|",
    ]
    for item in ncs["top_remaining_majors"]:
        lines.append(
            "| {major_code} {major_name} | {not_collected:,} | {api_failed:,} | {matched:,} | {total:,} |".format(
                **item
            )
        )
    lines.extend(
        [
            "",
            "## SQF DB Readiness",
            "",
            f"- Status: `{sqf['status']}`",
            f"- Allowed use: `{sqf['allowed_use']}`",
            f"- Official files: `{sqf['official_downloaded_count']} / {sqf['official_file_count']}`",
            f"- Pages/chunks: `{sqf['page_count']:,}` / `{sqf['chunk_count']:,}`",
            f"- SQF-NCS candidates: `{sqf['sqf_ncs_candidate_count']:,}`",
            f"- Claims/P0: `{sqf['claim_count']}` / `{sqf['p0_count']}`",
            f"- Approval ready: `{sqf['approval_ready']}`",
            f"- Used for scoring: `{sqf['used_for_scoring']}`",
            f"- Status update allowed: `{sqf['status_update_allowed']}`",
            "",
            "## Human Review Safe Ops",
            "",
            f"- SQF safe for reviewer evidence: `{review['sqf_safe_for_reviewer_evidence']}`",
            f"- SQF pending decisions: `{review['sqf_pending_decision_count']}`",
            f"- SQF guarded import candidates: `{review['sqf_guarded_import_candidate_count']}`",
            f"- SQF planned DB writes: `{review['sqf_planned_db_writes']}`",
            (
                "- Legacy trusted-status rows pending packet-backed reconfirmation: "
                f"`{review['legacy_trusted_status_rows_pending_reconfirmation']}`"
            ),
            f"- Rows without packet-backed provenance: `{review['rows_without_packet_backed_provenance']}`",
            f"- Provenance gap present: `{review['provenance_gap_present']}`",
            f"- Reconfirmation blank decisions: `{review['reconfirmation_blank_decision_count']}`",
            "",
            "## Operator Notes",
            "",
        ]
    )
    for note in checkpoint["operator_notes"]:
        lines.append(f"- {note}")
    lines.extend(["", "## Reviewer Artifact Priority", ""])
    for artifact in review["reviewer_safe_artifacts"][:10]:
        lines.append(f"- `{artifact}`")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a read-only overnight NCS/SQF work checkpoint.")
    parser.add_argument("--ncs006", type=Path, default=DEFAULT_NCS006)
    parser.add_argument("--sqf-db", type=Path, default=DEFAULT_SQF_DB)
    parser.add_argument("--human-review", type=Path, default=DEFAULT_HUMAN_REVIEW)
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
                "ok": checkpoint["ok"],
                "ncs006": checkpoint["ncs006_collection"],
                "sqf_db": checkpoint["sqf_db_readiness"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
