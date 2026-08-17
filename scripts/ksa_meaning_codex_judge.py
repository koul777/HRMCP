from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.config import load_settings
from ncs_mcp.db import connect, normalize_spaces, now_utc


APPROVABLE_SOURCE_METHODS = ("term_definition_template", "task_context_template")
DEFAULT_MEANING_ROLES = (
    "term_definition_candidate",
    "task_knowledge_significance",
    "task_skill_significance",
    "task_attitude_significance",
)
FORBIDDEN_STATUS_WRITES = {"human_reviewed", "accepted", "reviewed"}


def _chunks(values: list[int], size: int = 1000) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _reason_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        counts.update(str(reason) for reason in row.get("skip_reasons", []))
    return dict(sorted(counts.items()))


def _expected_task_role(concept_type: str) -> str:
    return {
        "knowledge": "task_knowledge_significance",
        "skill": "task_skill_significance",
        "attitude": "task_attitude_significance",
    }.get(concept_type, "task_ksa_significance")


def evaluate_meaning_candidate(
    row: dict[str, Any],
    *,
    min_confidence: float,
    min_text_length: int,
) -> tuple[bool, dict[str, Any]]:
    meaning_text = normalize_spaces(row.get("meaning_text") or "")
    evidence_text = normalize_spaces(row.get("evidence_text") or "")
    concept_name = normalize_spaces(row.get("concept_name") or "")
    concept_type = normalize_spaces(row.get("concept_type") or "")
    meaning_role = normalize_spaces(row.get("meaning_role") or "")
    source_method = normalize_spaces(row.get("source_method") or "")
    unit_code = normalize_spaces(row.get("unit_code") or "")
    criteria_id = row.get("criteria_id")
    confidence_score = row.get("confidence_score")
    try:
        confidence_value = float(confidence_score or 0.0)
    except (TypeError, ValueError):
        confidence_value = 0.0

    reasons: list[str] = []
    if confidence_value < min_confidence:
        reasons.append("confidence_too_low")
    if source_method not in APPROVABLE_SOURCE_METHODS:
        reasons.append("unsupported_source_method")
    if not concept_name:
        reasons.append("missing_concept_name")
    if concept_type not in {"knowledge", "skill", "attitude"}:
        reasons.append("unsupported_concept_type")
    if meaning_role not in DEFAULT_MEANING_ROLES:
        reasons.append("unsupported_meaning_role")
    if not meaning_text:
        reasons.append("missing_meaning_text")
    if len(meaning_text) < min_text_length:
        reasons.append("meaning_text_too_short")
    if not evidence_text:
        reasons.append("missing_evidence_text")
    if not unit_code:
        reasons.append("missing_unit_context")
    if meaning_role == "term_definition_candidate":
        if source_method != "term_definition_template":
            reasons.append("term_definition_source_mismatch")
    else:
        if source_method != "task_context_template":
            reasons.append("task_context_source_mismatch")
        if meaning_role != _expected_task_role(concept_type):
            reasons.append("task_role_type_mismatch")
        if criteria_id is None:
            reasons.append("missing_criteria_context")
    if concept_name and meaning_text and concept_name not in meaning_text:
        reasons.append("concept_name_not_in_meaning_text")

    return not reasons, {
        "meaning_id": int(row["meaning_id"]),
        "concept_id": int(row["concept_id"]),
        "concept_name": concept_name,
        "concept_type": concept_type,
        "meaning_role": meaning_role,
        "source_method": source_method,
        "confidence_score": confidence_value,
        "unit_code": unit_code,
        "criteria_id": criteria_id,
        "meaning_text": meaning_text,
        "evidence_text": evidence_text,
        "skip_reasons": reasons,
    }


def run_meaning_judge(
    *,
    db_path: Path,
    out: Path,
    manual_seedpack_out: Path,
    min_confidence: float,
    min_text_length: int,
    manual_limit: int,
    dry_run: bool,
    mark_skipped_needs_review: bool = False,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        role_placeholders = ",".join("?" for _ in DEFAULT_MEANING_ROLES)
        cursor = conn.execute(
            f"""
            SELECT
              kmc.meaning_id,
              kmc.concept_id,
              kmc.concept_type,
              kmc.meaning_role,
              kmc.meaning_text,
              kmc.source_method,
              kmc.evidence_text,
              kmc.unit_code,
              kmc.element_id,
              kmc.criteria_id,
              kmc.ksa_id,
              kmc.confidence_score,
              kmc.review_status,
              oc.concept_name
            FROM ksa_meaning_candidates kmc
            JOIN ontology_concepts oc ON oc.concept_id = kmc.concept_id
            WHERE kmc.review_status = 'candidate'
              AND kmc.meaning_role IN ({role_placeholders})
            ORDER BY kmc.confidence_score DESC, kmc.meaning_id
            """,
            DEFAULT_MEANING_ROLES,
        )
        approved: list[int] = []
        skipped: list[dict[str, Any]] = []
        manual_rows: list[dict[str, Any]] = []
        scanned = 0
        for row in cursor:
            scanned += 1
            ok, evidence = evaluate_meaning_candidate(
                dict(row),
                min_confidence=min_confidence,
                min_text_length=min_text_length,
            )
            if ok:
                approved.append(int(row["meaning_id"]))
            else:
                skipped.append(evidence)
                if len(manual_rows) < max(1, int(manual_limit)):
                    sample = dict(evidence)
                    sample["review_status"] = (
                        "needs_review"
                        if mark_skipped_needs_review and not dry_run
                        else "candidate"
                    )
                    sample["triage_target_review_status"] = "needs_review"
                    sample["allowed_decisions"] = ["approve", "needs_revision", "reject"]
                    sample["status_update_allowed"] = False
                    sample["trusted_status_write_allowed"] = False
                    sample["concept_definition_status_update_allowed"] = False
                    manual_rows.append(sample)

        skipped_reason_counts = _reason_counts(skipped)
        needs_review = [int(item["meaning_id"]) for item in skipped] if mark_skipped_needs_review else []
        timestamp = now_utc()
        if approved and not dry_run:
            for batch in _chunks(approved):
                conn.execute(
                    f"""
                    UPDATE ksa_meaning_candidates
                    SET review_status = 'llm_reviewed',
                        updated_at = ?
                    WHERE meaning_id IN ({",".join("?" for _ in batch)})
                      AND review_status = 'candidate'
                    """,
                    (timestamp, *batch),
                )
        if needs_review and not dry_run:
            for batch in _chunks(needs_review):
                conn.execute(
                    f"""
                    UPDATE ksa_meaning_candidates
                    SET review_status = 'needs_review',
                        updated_at = ?
                    WHERE meaning_id IN ({",".join("?" for _ in batch)})
                      AND review_status = 'candidate'
                    """,
                    (timestamp, *batch),
                )
        if (approved or needs_review) and not dry_run:
            conn.commit()

        manual_seedpack_out.parent.mkdir(parents=True, exist_ok=True)
        manual_seedpack_out.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in manual_rows)
            + ("\n" if manual_rows else ""),
            encoding="utf-8",
        )
        statuses_written: list[str] = []
        if approved and not dry_run:
            statuses_written.append("llm_reviewed")
        if needs_review and mark_skipped_needs_review and not dry_run:
            statuses_written.append("needs_review")
        forbidden_statuses_written = sorted(
            status for status in statuses_written if status in FORBIDDEN_STATUS_WRITES
        )
        report = {
            "ok": True,
            "schema": "ksa_meaning_codex_judge_v1",
            "generated_at": timestamp,
            "db_path": str(db_path),
            "dry_run": dry_run,
            "criteria": {
                "review_status": "candidate",
                "meaning_roles": list(DEFAULT_MEANING_ROLES),
                "approvable_source_methods": list(APPROVABLE_SOURCE_METHODS),
                "min_confidence": min_confidence,
                "min_text_length": min_text_length,
                "unit_context_required": True,
                "criteria_context_required_for_task_meanings": True,
                "concept_name_required_in_meaning_text": True,
                "concept_definition_status_update_allowed": False,
            },
            "candidate_rows_scanned": scanned,
            "auto_approved": len(approved),
            "skipped": len(skipped),
            "skipped_by_reason": skipped_reason_counts,
            "mark_skipped_needs_review": mark_skipped_needs_review,
            "needs_review": len(needs_review),
            "needs_review_by_reason": skipped_reason_counts if mark_skipped_needs_review else {},
            "needs_review_written": len(needs_review) if mark_skipped_needs_review and not dry_run else 0,
            "manual_seedpack_rows": len(manual_rows),
            "manual_seedpack_limit": max(1, int(manual_limit)),
            "manual_seedpack_total_eligible": len(skipped),
            "manual_seedpack_truncated": len(manual_rows) < len(skipped),
            "manual_seedpack_omitted_rows": max(0, len(skipped) - len(manual_rows)),
            "manual_seedpack_scope": "needs_review_top_sample",
            "manual_seedpack_path": str(manual_seedpack_out),
            "status_written": ",".join(statuses_written) if statuses_written else None,
            "statuses_written": statuses_written,
            "status_writes": {
                "llm_reviewed": len(approved) if not dry_run else 0,
                "needs_review": len(needs_review) if mark_skipped_needs_review and not dry_run else 0,
            },
            "forbidden_statuses_written": forbidden_statuses_written,
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex-as-judge auto review for KSA meaning candidates.")
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "reports" / f"ksa_meaning_codex_judge_{datetime.now().strftime('%Y%m%d')}.json",
    )
    parser.add_argument(
        "--manual-seedpack-out",
        type=Path,
        default=ROOT / "reports" / f"ksa_meaning_manual_review_{datetime.now().strftime('%Y%m%d')}.jsonl",
    )
    parser.add_argument("--min-confidence", type=float, default=0.70)
    parser.add_argument("--min-text-length", type=int, default=10)
    parser.add_argument("--manual-limit", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--mark-skipped-needs-review",
        action="store_true",
        help="Move skipped candidate meaning rows to needs_review instead of leaving them as candidate.",
    )
    args = parser.parse_args()

    settings = load_settings()
    db_path = args.db_path or settings.db_path
    report = run_meaning_judge(
        db_path=db_path,
        out=args.out,
        manual_seedpack_out=args.manual_seedpack_out,
        min_confidence=args.min_confidence,
        min_text_length=args.min_text_length,
        manual_limit=args.manual_limit,
        dry_run=args.dry_run,
        mark_skipped_needs_review=args.mark_skipped_needs_review,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
