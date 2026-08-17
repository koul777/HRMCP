from __future__ import annotations

import json
import sqlite3
import csv
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import ksa_label_quality_flags


LABEL_REPORT_SCHEMA = "ksa_label_candidate_report_v1"
LABEL_FAMILY_REPORT_SCHEMA = "ksa_short_label_family_report_v1"
LABEL_FAMILY_DECISION_SHEET_SCHEMA = "ksa_short_label_family_decision_sheet_v1"
LABEL_PATTERN_REPORT_SCHEMA = "ksa_short_label_pattern_report_v1"
LABEL_PATTERN_DECISION_SHEET_SCHEMA = "ksa_short_label_pattern_decision_sheet_v1"
LABEL_AUTO_TRIAGE_REPORT_SCHEMA = "ksa_label_auto_triage_report_v1"
LABEL_AUTO_TRIAGE_CSV_SCHEMA = "ksa_label_auto_triage_decision_sheet_v1"
LABEL_POLICY_V2_SAMPLING_PLAN_SCHEMA = "ksa_label_policy_v2_operator_sampling_plan_v1"
LABEL_POLICY_V2_SAMPLING_PLAN_CSV_SCHEMA = (
    "ksa_label_policy_v2_operator_sampling_plan_decision_sheet_v1"
)
LABEL_POLICY_V2_SCOPE_DIFF_SCHEMA = "ksa_label_policy_v2_scope_diff_v1"
LABEL_MISSING_GAP_REVIEW_PACK_SCHEMA = "ksa_missing_label_gap_review_pack_v1"
LABEL_MISSING_GAP_CSV_SCHEMA = "ksa_missing_label_gap_decision_sheet_v1"
TRUSTED_REVIEW_STATUSES = ("human_reviewed", "accepted", "reviewed")
AUTOMATED_REVIEWER_IDS = ("dashboard", "mcp", "automation", "automated_eval_gate", "system")
KSA_LABEL_REVIEW_DECISIONS = ("approve", "needs_revision", "reject")
QUALITY_FLAG_PRIORITY = (
    "unbalanced_parentheses",
    "dangling_enum_suffix",
    "skill_suffix_stripped_to_generic",
    "very_low_label_source_ratio",
    "symbol_heavy",
    "digit_heavy",
    "short_acronym_needs_context",
    "changed_near_full_length",
    "generic_or_low_specificity",
)
GENERIC_LABEL_KEYS = {
    "관리",
    "분석",
    "계획",
    "평가",
    "운영",
    "작성",
    "수립",
    "검토",
    "활용",
    "지원",
    "이해",
    "처리",
    "능력",
    "기술",
    "지식",
    "태도",
    "기준",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def _group_counts(
    conn: sqlite3.Connection,
    column: str,
    *,
    where_sql: str = "",
    params: tuple[Any, ...] = (),
) -> dict[str, int]:
    rows = conn.execute(
        f"""
        SELECT COALESCE({column}, '') AS key, COUNT(*) AS count
        FROM ontology_concept_label_candidates label
        {where_sql}
        GROUP BY COALESCE({column}, '')
        ORDER BY count DESC, key
        """,
        params,
    ).fetchall()
    return {str(row["key"] if isinstance(row, sqlite3.Row) else row[0]): int(row["count"] if isinstance(row, sqlite3.Row) else row[1]) for row in rows}


def _clip(value: Any, limit: int = 180) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def _short_label_transform_metrics(source_text: Any, label_text: Any) -> dict[str, Any]:
    source = str(source_text or "").strip()
    label = str(label_text or "").strip()
    source_len = len(source)
    label_len = len(label)
    if not label:
        state = "missing"
    elif not source:
        state = "source_missing"
    elif source == label:
        state = "unchanged"
    elif label_len < source_len:
        state = "shortened"
    else:
        state = "expanded_or_rewritten"
    return {
        "short_label_transform_state": state,
        "short_label_source_length": source_len if source else None,
        "short_label_label_length": label_len if label else None,
        "short_label_removed_char_count": max(source_len - label_len, 0)
        if source and label
        else None,
        "short_label_length_ratio": round(label_len / source_len, 3)
        if source_len and label
        else None,
    }


def _label_method_details_from_evidence(evidence_text: Any) -> str:
    text = str(evidence_text or "")
    match = re.search(r"(?:^|\|\s*)method_details:\s*(.*)$", text)
    return match.group(1).strip() if match else ""


def _source_text_payload(row: sqlite3.Row) -> dict[str, Any]:
    raw_ksa_text = row["raw_ksa_text"]
    atomic_ksa_text = row["atomic_ksa_text"]
    if raw_ksa_text is None and row["source_atomic_id"] is None and row["source_ksa_id"] is not None:
        raw_ksa_text = row["source_text"]
    return {
        "raw_ksa_text": raw_ksa_text,
        "atomic_ksa_text": atomic_ksa_text,
    }


def _label_quality_flag_summary(
    conn: sqlite3.Connection,
    where_sql: str,
    params: tuple[Any, ...],
    *,
    example_limit: int = 20,
) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]]]:
    rows = conn.execute(
        f"""
        SELECT
            label.label_id,
            label.concept_id,
            label.concept_type,
            label.source_scope_key,
            label.source_text,
            label.label_text,
            label.source_method,
            label.confidence_score
        FROM ontology_concept_label_candidates label
        {where_sql}
        ORDER BY label.label_id
        """,
        params,
    ).fetchall()
    counts: dict[str, int] = {}
    examples: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        flags = ksa_label_quality_flags(
            row["source_text"] or "",
            row["label_text"] or "",
            row["concept_type"] or "",
        )
        for flag in flags:
            counts[flag] = counts.get(flag, 0) + 1
            bucket = examples.setdefault(flag, [])
            if len(bucket) < example_limit:
                bucket.append(
                    {
                        "label_id": int(row["label_id"]),
                        "concept_id": int(row["concept_id"]),
                        "concept_type": row["concept_type"],
                        "source_scope_key": row["source_scope_key"],
                        "source_text": _clip(row["source_text"], 220),
                        "label_text": row["label_text"],
                        "source_method": row["source_method"],
                        "confidence_score": float(row["confidence_score"] or 0.0),
                    }
                )
    return counts, examples


def _label_scope(major_code: str | None) -> tuple[str, tuple[Any, ...]]:
    if not major_code:
        return "", ()
    return (
        """
        WHERE (
            EXISTS (
                SELECT 1
                FROM ksa_items ki
                JOIN competency_elements ce ON ce.element_id = ki.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                WHERE ki.ksa_id = label.source_ksa_id
                  AND c.major_code = ?
            )
            OR EXISTS (
                SELECT 1
                FROM ksa_atomic_items atom
                JOIN competency_elements ce ON ce.element_id = atom.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                WHERE atom.atomic_id = label.source_atomic_id
                  AND c.major_code = ?
            )
        )
        """,
        (major_code, major_code),
    )


def _trusted_review_status_filter() -> tuple[str, tuple[Any, ...]]:
    return (
        f"label.review_status IN ({','.join('?' for _ in TRUSTED_REVIEW_STATUSES)})",
        TRUSTED_REVIEW_STATUSES,
    )


def _audited_label_review_exists_sql() -> str:
    automated_placeholders = ",".join("?" for _ in AUTOMATED_REVIEWER_IDS)
    return f"""
        EXISTS (
            SELECT 1
            FROM review_audit_log audit
            WHERE audit.entity_type = 'ontology_concept_label_candidate'
              AND audit.entity_id = CAST(label.label_id AS TEXT)
              AND audit.new_status = label.review_status
              AND TRIM(COALESCE(audit.reviewer_id, '')) <> ''
              AND LOWER(TRIM(audit.reviewer_id)) NOT IN ({automated_placeholders})
              AND TRIM(COALESCE(audit.notes, '')) <> ''
              AND TRIM(COALESCE(audit.source_decision_packet, '')) <> ''
              AND TRIM(COALESCE(audit.rationale, '')) <> ''
        )
    """


def _trusted_label_review_status_placeholders() -> str:
    return ",".join("?" for _ in TRUSTED_REVIEW_STATUSES)


def _unbalanced_parentheses_sql(column: str) -> str:
    checks = []
    for opening, closing in (
        ("(", ")"),
        ("[", "]"),
        ("{", "}"),
        ("（", "）"),
        ("【", "】"),
        ("「", "」"),
        ("｢", "｣"),
    ):
        checks.append(
            f"LENGTH({column}) - LENGTH(REPLACE({column}, '{opening}', '')) != "
            f"LENGTH({column}) - LENGTH(REPLACE({column}, '{closing}', ''))"
        )
    return "(" + "\n                   OR ".join(checks) + ")"


def _label_anomaly_scope(major_code: str | None) -> tuple[str, tuple[Any, ...]]:
    if not major_code:
        return "", ()
    return (
        """
        WHERE (
            EXISTS (
                SELECT 1
                FROM ksa_items ki
                JOIN competency_elements ce ON ce.element_id = ki.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                WHERE ki.ksa_id = label.source_ksa_id
                  AND c.major_code = ?
            )
            OR EXISTS (
                SELECT 1
                FROM ksa_atomic_items atom
                JOIN competency_elements ce ON ce.element_id = atom.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                WHERE atom.atomic_id = label.source_atomic_id
                  AND c.major_code = ?
            )
            OR (
                label.source_ksa_id IS NULL
                AND label.source_atomic_id IS NULL
                AND (
                    EXISTS (
                        SELECT 1
                        FROM ksa_concept_links kcl
                        JOIN ksa_items ki ON ki.ksa_id = kcl.ksa_id
                        JOIN competency_elements ce ON ce.element_id = ki.element_id
                        JOIN competency_units cu ON cu.unit_code = ce.unit_code
                        JOIN classifications c ON c.classification_id = cu.classification_id
                        WHERE kcl.concept_id = label.concept_id
                          AND c.major_code = ?
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM ksa_atomic_concept_links acl
                        JOIN ksa_atomic_items atom ON atom.atomic_id = acl.atomic_id
                        JOIN competency_elements ce ON ce.element_id = atom.element_id
                        JOIN competency_units cu ON cu.unit_code = ce.unit_code
                        JOIN classifications c ON c.classification_id = cu.classification_id
                        WHERE acl.concept_id = label.concept_id
                          AND c.major_code = ?
                    )
                )
            )
        )
        """,
        (major_code, major_code, major_code, major_code),
    )


def _label_where_with(where_sql: str, condition: str) -> str:
    if where_sql:
        return f"{where_sql}\n        AND ({condition})"
    return f"WHERE ({condition})"


def _review_prompt_for_label_issue(
    issue_type: str,
    quality_flags: list[str],
    row: sqlite3.Row,
) -> str:
    label_text = row["label_text"] or ""
    source_text = row["source_text"] or ""
    collision_count = int(row["collision_concept_count"] or 0)
    if issue_type == "collision":
        return (
            f"'{label_text}' label maps to {collision_count} concepts in this scope. "
            "Decide whether to merge concepts, keep aliases, or choose a more specific representative label."
        )
    if "short_acronym_needs_context" in quality_flags:
        return (
            f"'{label_text}' is a short acronym. Check whether the acronym is sufficient "
            "or should keep domain context from the source KSA."
        )
    if "very_low_label_source_ratio" in quality_flags:
        return (
            f"'{label_text}' is much shorter than its source. Verify that the candidate "
            f"still preserves the meaning of '{_clip(source_text, 80)}'."
        )
    if "changed_near_full_length" in quality_flags:
        return (
            "The candidate is almost the same length as the source. Confirm whether it is "
            "a useful term-style label or should remain unchanged."
        )
    if "generic_or_low_specificity" in quality_flags or issue_type == "generic":
        return (
            f"'{label_text}' may be too generic. Prefer a label that carries the task or domain-specific meaning."
        )
    if "unbalanced_parentheses" in quality_flags or "dangling_enum_suffix" in quality_flags:
        return "The candidate appears syntactically damaged. Repair the label before any trusted status update."
    if issue_type == "low_confidence":
        return "Low confidence candidate. Compare source and label before accepting or replacing it."
    return "Review whether the short label accurately represents the source KSA without changing raw evidence."


def _review_focus_for_label_issue(issue_type: str, quality_flags: list[str]) -> list[str]:
    focus: list[str] = [
        "raw_ksa_to_atomic_ksa",
        "atomic_ksa_to_representative_concept",
        "representative_concept_to_short_label",
    ]
    if issue_type == "collision":
        focus.extend(
            [
                "normalized_label_collision",
                "merge_or_alias_decision",
                "label_scope_specificity",
            ]
        )
    if "short_acronym_needs_context" in quality_flags:
        focus.extend(["acronym_expansion_preservation", "domain_context_check"])
    if "very_low_label_source_ratio" in quality_flags:
        focus.extend(["meaning_preservation", "dropped_context_check"])
    if "changed_near_full_length" in quality_flags:
        focus.extend(["term_style_usefulness", "unchanged_label_preference"])
    if "generic_or_low_specificity" in quality_flags or issue_type == "generic":
        focus.extend(["task_or_domain_context_required", "specificity_check"])
    if "unbalanced_parentheses" in quality_flags or "dangling_enum_suffix" in quality_flags:
        focus.extend(["syntax_repair_required", "do_not_approve_until_fixed"])
    if issue_type == "low_confidence":
        focus.extend(["source_label_comparison", "replacement_label_needed"])
    if len(focus) == 3:
        focus.extend(["source_label_equivalence", "raw_evidence_preserved"])
    return list(dict.fromkeys(focus))


def _seedpack_term_definition_evidence(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> dict[str, Any]:
    concept_id = int(row["concept_id"])
    source_ksa_id = row["source_ksa_id"]
    term_row = conn.execute(
        """
        SELECT
          meaning_role,
          meaning_text,
          evidence_text,
          confidence_score,
          review_status,
          criteria_id
        FROM ksa_meaning_candidates
        WHERE concept_id = ?
          AND source_method = 'term_definition_template'
        ORDER BY
          CASE WHEN ksa_id = ? THEN 0 ELSE 1 END,
          CASE WHEN criteria_id IS NULL THEN 1 ELSE 0 END,
          confidence_score DESC,
          meaning_id
        LIMIT 1
        """,
        (concept_id, source_ksa_id),
    ).fetchone()
    if term_row is None:
        return {
            "term_definition_candidate": None,
            "term_definition_evidence": None,
            "term_definition_role": None,
            "term_definition_review_status": None,
            "term_definition_confidence": None,
        }
    return {
        "term_definition_candidate": term_row["meaning_text"],
        "term_definition_evidence": term_row["evidence_text"],
        "term_definition_role": term_row["meaning_role"],
        "term_definition_review_status": term_row["review_status"],
        "term_definition_confidence": float(term_row["confidence_score"]),
    }


def _seedpack_task_evidence(conn: sqlite3.Connection, concept_id: int) -> dict[str, Any]:
    evidence_sql = """
        WITH evidence AS (
          SELECT
            'ksa_meaning_candidates.' || kmc.source_method AS evidence_ref,
            kmc.criteria_id,
            pc.criteria_text_raw AS criteria_text,
            kmc.evidence_text,
            kmc.meaning_text,
            kmc.review_status,
            kmc.confidence_score
          FROM ksa_meaning_candidates kmc
          LEFT JOIN performance_criteria pc ON pc.criteria_id = kmc.criteria_id
          WHERE kmc.concept_id = ?
            AND kmc.source_method != 'term_definition_template'
          UNION ALL
          SELECT
            'criteria_concept_links.' || ccl.relation_type AS evidence_ref,
            ccl.criteria_id,
            pc.criteria_text_raw AS criteria_text,
            NULL AS evidence_text,
            NULL AS meaning_text,
            ccl.link_status AS review_status,
            NULL AS confidence_score
          FROM criteria_concept_links ccl
          JOIN performance_criteria pc ON pc.criteria_id = ccl.criteria_id
          WHERE ccl.concept_id = ?
          UNION ALL
          SELECT
            'task_ksa_concept_relations.' || rel.relation_type AS evidence_ref,
            rel.criteria_id,
            pc.criteria_text_raw AS criteria_text,
            rel.evidence_text,
            NULL AS meaning_text,
            rel.review_status,
            rel.confidence_score
          FROM task_ksa_concept_relations rel
          JOIN performance_criteria pc ON pc.criteria_id = rel.criteria_id
          WHERE rel.source_concept_id = ?
             OR rel.target_concept_id = ?
        )
    """
    count_row = conn.execute(
        f"{evidence_sql} SELECT COUNT(*) FROM evidence",
        (concept_id, concept_id, concept_id, concept_id),
    ).fetchone()
    rows = conn.execute(
        f"""
        {evidence_sql}
        SELECT *
        FROM evidence
        ORDER BY
          CASE WHEN criteria_id IS NULL THEN 1 ELSE 0 END,
          COALESCE(confidence_score, 0) DESC,
          evidence_ref,
          criteria_id
        LIMIT 20
        """,
        (concept_id, concept_id, concept_id, concept_id),
    ).fetchall()
    criteria_ids: list[int] = []
    criteria_texts: list[str] = []
    previews: list[str] = []
    refs: list[str] = []
    seen_previews: set[str] = set()
    seen_refs: set[str] = set()
    for evidence in rows:
        criteria_id = evidence["criteria_id"]
        if criteria_id is not None and int(criteria_id) not in criteria_ids:
            criteria_ids.append(int(criteria_id))
        criteria_text = _clip(evidence["criteria_text"], 140)
        if criteria_text and criteria_text not in criteria_texts:
            criteria_texts.append(criteria_text)
        ref = str(evidence["evidence_ref"] or "")
        if criteria_id is not None:
            ref = f"{ref}#criteria:{int(criteria_id)}"
        if ref not in seen_refs:
            refs.append(ref)
            seen_refs.add(ref)
        detail = evidence["evidence_text"] or evidence["meaning_text"] or evidence["criteria_text"] or ""
        preview = f"{ref}: {_clip(detail, 180)}"
        if preview not in seen_previews:
            previews.append(preview)
            seen_previews.add(preview)
    return {
        "task_evidence_count": int(count_row[0] or 0),
        "task_evidence_preview": previews[:5],
        "task_evidence_refs": refs[:5],
        "criteria_ids": criteria_ids,
        "criteria_text_preview": criteria_texts[:3],
    }


def _ksa_scope_join(major_code: str | None) -> tuple[str, tuple[Any, ...]]:
    if not major_code:
        return "", ()
    return (
        """
        JOIN competency_elements ce ON ce.element_id = ki.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE c.major_code = ?
        """,
        (major_code,),
    )


def _atomic_scope_join(major_code: str | None) -> tuple[str, tuple[Any, ...]]:
    if not major_code:
        return "", ()
    return (
        """
        JOIN competency_elements ce ON ce.element_id = atom.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE c.major_code = ?
        """,
        (major_code,),
    )


def _ontology_concept_scope(major_code: str | None) -> tuple[str, tuple[Any, ...]]:
    if not major_code:
        return "", ()
    return (
        """
        WHERE (
            EXISTS (
                SELECT 1
                FROM ksa_concept_links kcl
                JOIN ksa_items ki ON ki.ksa_id = kcl.ksa_id
                JOIN competency_elements ce ON ce.element_id = ki.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                WHERE kcl.concept_id = oc.concept_id
                  AND c.major_code = ?
            )
            OR EXISTS (
                SELECT 1
                FROM ksa_atomic_concept_links acl
                JOIN ksa_atomic_items atom ON atom.atomic_id = acl.atomic_id
                JOIN competency_elements ce ON ce.element_id = atom.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                WHERE acl.concept_id = oc.concept_id
                  AND c.major_code = ?
            )
        )
        """,
        (major_code, major_code),
    )


def build_ksa_label_candidate_report(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    sample_limit: int = 20,
    collision_limit: int = 20,
) -> dict[str, Any]:
    """Build a read-only report for short KSA representative label candidates."""
    label_where, label_params = _label_scope(major_code)
    label_anomaly_where, label_anomaly_params = _label_anomaly_scope(major_code)
    ksa_join, ksa_params = _ksa_scope_join(major_code)
    atomic_join, atomic_params = _atomic_scope_join(major_code)
    concept_where, concept_params = _ontology_concept_scope(major_code)
    sample_limit = max(1, min(int(sample_limit or 20), 200))
    collision_limit = max(1, min(int(collision_limit or 20), 200))

    label_count = _scalar(
        conn,
        f"SELECT COUNT(*) FROM ontology_concept_label_candidates label {label_where}",
        label_params,
    )
    trusted_filter, trusted_params = _trusted_review_status_filter()
    audited_label_review_exists_sql = _audited_label_review_exists_sql()
    trusted_unaudited_filter = f"{trusted_filter} AND NOT {audited_label_review_exists_sql}"
    trusted_audited_filter = f"{trusted_filter} AND {audited_label_review_exists_sql}"
    counts = {
        "raw_ksa_items": _scalar(conn, f"SELECT COUNT(*) FROM ksa_items ki {ksa_join}", ksa_params),
        "atomic_ksa_items": _scalar(
            conn,
            f"SELECT COUNT(*) FROM ksa_atomic_items atom {atomic_join}",
            atomic_params,
        ),
        "ontology_concepts": _scalar(
            conn,
            f"SELECT COUNT(*) FROM ontology_concepts oc {concept_where}",
            concept_params,
        ),
        "global_ontology_concepts": _scalar(conn, "SELECT COUNT(*) FROM ontology_concepts"),
        "label_candidates": label_count,
        "concepts_with_label_candidates": _scalar(
            conn,
            f"""
            SELECT COUNT(DISTINCT label.concept_id)
            FROM ontology_concept_label_candidates label
            {label_where}
            """,
            label_params,
        ),
        "shortened_label_candidates": _scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates label
            {_label_where_with(label_where, "label.source_method = 'rule_based_short_label_candidate'")}
            """,
            label_params,
        ),
        "unchanged_label_candidates": _scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates label
            {_label_where_with(label_where, "label.source_method = 'already_short_label'")}
            """,
            label_params,
        ),
        "labels_shorter_than_source": _scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates label
            {_label_where_with(label_where, "LENGTH(TRIM(label.label_text)) < LENGTH(TRIM(label.source_text))")}
            """,
            label_params,
        ),
        "label_matches_concept_name": _scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates label
            JOIN ontology_concepts oc ON oc.concept_id = label.concept_id
            {_label_where_with(label_where, "TRIM(label.label_text) = TRIM(oc.concept_name)")}
            """,
            label_params,
        ),
        "missing_source_id_rows": _scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates label
            {_label_where_with(label_anomaly_where, "label.source_ksa_id IS NULL AND label.source_atomic_id IS NULL")}
            """,
            label_anomaly_params,
        ),
        "missing_text_rows": _scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates label
            {_label_where_with(label_anomaly_where, "TRIM(COALESCE(label.source_text, '')) = '' OR TRIM(COALESCE(label.label_text, '')) = ''")}
            """,
            label_anomaly_params,
        ),
        "trusted_status_total_rows": _scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates label
            {_label_where_with(label_anomaly_where, trusted_filter)}
            """,
            (*label_anomaly_params, *trusted_params),
        ),
        "trusted_status_rows": _scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates label
            {_label_where_with(label_anomaly_where, trusted_unaudited_filter)}
            """,
            (*label_anomaly_params, *trusted_params, *AUTOMATED_REVIEWER_IDS),
        ),
        "audited_trusted_status_rows": _scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates label
            {_label_where_with(label_anomaly_where, trusted_audited_filter)}
            """,
            (*label_anomaly_params, *trusted_params, *AUTOMATED_REVIEWER_IDS),
        ),
        "source_preservation_violations": _scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates label
            {_label_where_with(label_anomaly_where, "(label.source_ksa_id IS NULL AND label.source_atomic_id IS NULL) OR TRIM(COALESCE(label.source_text, '')) = '' OR TRIM(COALESCE(label.label_text, '')) = ''")}
            """,
            label_anomaly_params,
        ),
    }

    by_method = _group_counts(
        conn,
        "label.source_method",
        where_sql=label_where,
        params=label_params,
    )
    by_type = _group_counts(
        conn,
        "label.concept_type",
        where_sql=label_where,
        params=label_params,
    )
    by_review_status = _group_counts(
        conn,
        "label.review_status",
        where_sql=label_where,
        params=label_params,
    )

    collision_rows = conn.execute(
        f"""
        SELECT
            label.normalized_label_key,
            MIN(label.label_text) AS sample_label,
            COUNT(*) AS row_count,
            COUNT(DISTINCT label.concept_id) AS concept_count,
            GROUP_CONCAT(DISTINCT label.concept_type) AS concept_types
        FROM ontology_concept_label_candidates label
        {label_where}
        GROUP BY label.normalized_label_key
        HAVING COUNT(DISTINCT label.concept_id) > 1
        ORDER BY concept_count DESC, row_count DESC, sample_label
        LIMIT ?
        """,
        (*label_params, collision_limit),
    ).fetchall()
    collision_examples = [
        {
            "normalized_label_key": row["normalized_label_key"],
            "sample_label": row["sample_label"],
            "row_count": int(row["row_count"]),
            "concept_count": int(row["concept_count"]),
            "concept_types": (row["concept_types"] or "").split(",") if row["concept_types"] else [],
        }
        for row in collision_rows
    ]

    _generic_extra_where = (
        "\n            LENGTH(TRIM(label.label_text)) <= 3"
        "\n            OR label.normalized_label_key IN ("
        "\n                'knowledge', 'skill', 'attitude', 'management', 'analysis',"
        "\n                'planning', 'operation', 'support', 'communication'"
        "\n            )"
    )
    _generic_where_clause = _label_where_with(label_where, _generic_extra_where)
    generic_rows = conn.execute(
        f"""
        SELECT
            label.label_text,
            label.normalized_label_key,
            label.concept_type,
            COUNT(*) AS row_count,
            COUNT(DISTINCT label.concept_id) AS concept_count
        FROM ontology_concept_label_candidates label
        {_generic_where_clause}
        GROUP BY label.label_text, label.normalized_label_key, label.concept_type
        ORDER BY concept_count DESC, row_count DESC, label.label_text
        LIMIT ?
        """,
        (*label_params, collision_limit),
    ).fetchall()
    generic_examples = [
        {
            "label_text": row["label_text"],
            "normalized_label_key": row["normalized_label_key"],
            "concept_type": row["concept_type"],
            "row_count": int(row["row_count"]),
            "concept_count": int(row["concept_count"]),
        }
        for row in generic_rows
    ]
    quality_flag_counts, quality_flag_examples = _label_quality_flag_summary(
        conn,
        label_where,
        label_params,
        example_limit=collision_limit,
    )

    sample_rows = conn.execute(
        f"""
        SELECT
            label.label_id,
            label.concept_id,
            label.concept_type,
            oc.concept_name,
            label.source_ksa_id,
            label.source_atomic_id,
            source_ki.ksa_text_raw AS raw_ksa_text,
            source_atom.atom_text AS atomic_ksa_text,
            label.source_scope_key,
            label.source_text,
            label.label_text,
            label.source_method,
            label.confidence_score,
            label.review_status
        FROM ontology_concept_label_candidates label
        JOIN ontology_concepts oc ON oc.concept_id = label.concept_id
        LEFT JOIN ksa_atomic_items source_atom ON source_atom.atomic_id = label.source_atomic_id
        LEFT JOIN ksa_items source_ki ON source_ki.ksa_id = COALESCE(source_atom.ksa_id, label.source_ksa_id)
        {label_where}
        ORDER BY
            CASE label.source_method
                WHEN 'rule_based_short_label_candidate' THEN 0
                ELSE 1
            END,
            label.confidence_score DESC,
            label.label_id
        LIMIT ?
        """,
        (*label_params, sample_limit),
    ).fetchall()
    samples = [
        {
            "label_id": int(row["label_id"]),
            "concept_id": int(row["concept_id"]),
            "concept_type": row["concept_type"],
            "concept_name": row["concept_name"],
            "source_ksa_id": row["source_ksa_id"],
            "source_atomic_id": row["source_atomic_id"],
            **_source_text_payload(row),
            "source_scope_key": row["source_scope_key"],
            "source_text": _clip(row["source_text"], 260),
            "short_label_candidate": row["label_text"],
            **_short_label_transform_metrics(row["source_text"], row["label_text"]),
            "source_method": row["source_method"],
            "confidence_score": float(row["confidence_score"]),
            "review_status": row["review_status"],
        }
        for row in sample_rows
    ]

    ok = (
        counts["missing_source_id_rows"] == 0
        and counts["missing_text_rows"] == 0
        and counts["trusted_status_rows"] == 0
    )
    return {
        "schema": LABEL_REPORT_SCHEMA,
        "generated_at": _now_iso(),
        "ok": ok,
        "status": "ok" if ok else "review_required",
        "scope": {
            "major_code": major_code,
            "sample_limit": sample_limit,
            "collision_limit": collision_limit,
        },
        "status_update_allowed": False,
        "pipeline_contract": [
            {
                "stage": "raw_ksa",
                "table": "ksa_items",
                "field": "ksa_text_raw",
                "contract": "source text is preserved",
            },
            {
                "stage": "atomic_ksa",
                "table": "ksa_atomic_items",
                "field": "atom_text",
                "contract": "split candidate, still not a representative term",
            },
            {
                "stage": "representative_concept",
                "table": "ontology_concepts",
                "field": "concept_name",
                "contract": "representative concept node, not overwritten by short labels",
            },
            {
                "stage": "short_label_candidate",
                "display_label": "단어형 대표 라벨 후보",
                "table": "ontology_concept_label_candidates",
                "field": "label_text",
                "contract": "short representative label candidate only; review context",
            },
            {
                "stage": "label_source_provenance",
                "table": "ontology_concept_label_candidates",
                "field": "source_ksa_id/source_atomic_id/source_scope_key/source_text",
                "contract": "candidate label provenance must keep raw and atomic KSA context visible",
            },
            {
                "stage": "term_definition_candidate",
                "table": "ksa_meaning_candidates",
                "field": "meaning_text",
                "contract": "term-definition candidate evidence only; not an approved ontology definition",
            },
            {
                "stage": "task_evidence_candidate",
                "table": "task_ksa_concept_relations / criteria_concept_links",
                "field": "criteria_id/concept_id/evidence_text",
                "contract": "links concept evidence back to task criteria",
            },
            {
                "stage": "human_review_state",
                "table": "ontology_concept_label_candidates / review_audit_log",
                "field": "review_status/reviewer_id/notes",
                "contract": "trusted statuses require explicit human review evidence; automation cannot approve",
            },
        ],
        "counts": counts,
        "distributions": {
            "by_source_method": by_method,
            "by_concept_type": by_type,
            "by_review_status": by_review_status,
        },
        "quality": {
            "collision_example_count": len(collision_examples),
            "generic_example_count": len(generic_examples),
            "collision_examples": collision_examples,
            "generic_examples": generic_examples,
            "quality_flag_counts": quality_flag_counts,
            "quality_flag_examples": quality_flag_examples,
        },
        "samples": samples,
        "notes": [
            "Short label candidates are additive review context; they do not overwrite raw KSA or ontology_concepts.concept_name.",
            "Label candidates are source-scope aware through ontology_concept_label_candidates.source_scope_key.",
            "Provenance-less label rows are counted as anomalies but excluded from valid samples and review seedpacks.",
            "Trusted label statuses are allowed only when backed by review_audit_log human-review evidence.",
            "Candidate meaning evidence remains in ksa_meaning_candidates and is separate from short label candidates.",
        ],
    }


def _label_family_risk_score(
    *,
    normalized_label_key: str,
    row_count: int,
    concept_count: int,
    scope_count: int,
    major_count: int,
    status_counts: dict[str, int],
    audited_trusted_review_count: int,
    unaudited_trusted_review_count: int,
    quality_flag_counts: dict[str, int],
    missing_provenance_count: int,
    label_variant_count: int,
) -> tuple[int, list[str], str]:
    score = 0
    reasons: list[str] = []
    needs_review_count = int(status_counts.get("needs_review") or 0)
    rejected_count = int(status_counts.get("rejected") or 0)

    if needs_review_count:
        score += min(60, 12 + needs_review_count)
        reasons.append("contains_needs_review_rows")
    if rejected_count:
        score += min(40, 10 + rejected_count)
        reasons.append("contains_rejected_rows")
    if missing_provenance_count:
        score += 45
        reasons.append("missing_source_provenance")
    if audited_trusted_review_count:
        score += 30
        reasons.append("contains_audit_backed_trusted_review")
    if unaudited_trusted_review_count:
        score += 35
        reasons.append("contains_unaudited_trusted_status")
    if concept_count >= 20:
        score += 28
        reasons.append("same_label_maps_to_many_concepts")
    elif concept_count >= 5:
        score += 18
        reasons.append("same_label_maps_to_multiple_concepts")
    if scope_count >= 100:
        score += 24
        reasons.append("very_broad_cross_scope_label")
    elif scope_count >= 25:
        score += 12
        reasons.append("broad_cross_scope_label")
    if major_count >= 8:
        score += 18
        reasons.append("cross_major_label")
    elif major_count >= 3:
        score += 8
        reasons.append("multi_major_label")
    if label_variant_count > 1:
        score += min(12, label_variant_count)
        reasons.append("multiple_display_label_variants")
    if normalized_label_key in GENERIC_LABEL_KEYS or len(normalized_label_key) <= 2:
        score += 24
        reasons.append("generic_or_too_short_family_label")

    for flag, count in quality_flag_counts.items():
        if count <= 0:
            continue
        if flag in {
            "generic_or_low_specificity",
            "skill_suffix_stripped_to_generic",
            "short_acronym_needs_context",
        }:
            score += min(36, 10 + count)
        elif flag in {"very_low_label_source_ratio", "changed_near_full_length"}:
            score += min(24, 6 + count)
        else:
            score += min(18, 4 + count)
        reasons.append(f"quality_flag:{flag}")

    if row_count >= 100 and not reasons:
        score += 6
        reasons.append("high_repetition_machine_screened_spotcheck")

    if score >= 80:
        level = "critical_label_family_review"
    elif score >= 50:
        level = "high_label_family_review"
    elif score >= 20:
        level = "medium_label_family_review"
    else:
        level = "machine_screened_spotcheck_only"
    return score, reasons, level


def _top_counter_items(counter: Counter[str], limit: int = 5) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def _label_pattern_for_review(
    source_text: Any,
    label_text: Any,
    concept_type: str,
    quality_flags: list[str],
) -> tuple[str, str]:
    source = str(source_text or "").strip()
    label = str(label_text or "").strip()
    ratio = (len(label) / len(source)) if source else 0.0
    flags = set(quality_flags)
    if "short_acronym_needs_context" in flags:
        return "short_acronym_context_check", "Add source context before accepting short acronyms."
    if "symbol_heavy" in flags or "digit_heavy" in flags:
        return "symbol_or_digit_heavy_check", "Check whether technical codes are meaningful labels or need context."
    if "generic_or_low_specificity" in flags or len(label) <= 3:
        return "generic_or_too_short_label", "Avoid approving broad labels unless the source term is already narrow."
    if "very_low_label_source_ratio" in flags or ratio < 0.45:
        return "large_compression_check", "Check whether the compressed label preserves the source KSA meaning."
    if "changed_near_full_length" in flags or ratio >= 0.9:
        if concept_type == "attitude":
            return "near_full_attitude_word_normalization", "Spot-check wording such as 의지/자세/노력 -> 태도."
        if concept_type == "skill":
            return "near_full_skill_phrase_cleanup", "Check that ability/skill wording was not cut into an incomplete phrase."
        return "near_full_knowledge_phrase_cleanup", "Spot-check particle/descriptor cleanup against the source term."
    if concept_type == "skill" and "skill_suffix_stripped_to_generic" in flags:
        return "skill_suffix_generic_check", "Do not accept skill labels that became generic after suffix stripping."
    return "other_needs_review_pattern", "Inspect samples before choosing a rule-level handling."


def _label_pattern_review_policy(pattern_name: str) -> dict[str, Any]:
    """Return read-only operator policy for a remaining needs_review transform pattern."""
    if pattern_name == "short_acronym_context_check":
        return {
            "automation_recommendation": "keep_human_review",
            "minimum_review_unit": "top_acronym_families_plus_source_context",
            "operator_decision_hint": "Check the acronym with source context before accepting it as an ontology label.",
            "decision_options": [
                "accept_acronym_with_context",
                "expand_acronym_label",
                "keep_needs_review",
            ],
        }
    if pattern_name == "generic_or_too_short_label":
        return {
            "automation_recommendation": "keep_human_review",
            "minimum_review_unit": "top_label_families_plus_samples",
            "operator_decision_hint": "Broad or very short labels should not be auto-approved without source meaning.",
            "decision_options": [
                "accept_if_source_term_is_narrow",
                "merge_or_downweight_generic_label",
                "keep_needs_review",
            ],
        }
    if pattern_name == "large_compression_check":
        return {
            "automation_recommendation": "keep_human_review",
            "minimum_review_unit": "pattern_samples_and_top_label_families",
            "operator_decision_hint": "Verify that compression did not remove the core KSA meaning.",
            "decision_options": [
                "accept_compression_pattern",
                "retune_rule_for_pattern",
                "keep_needs_review",
            ],
        }
    if pattern_name == "symbol_or_digit_heavy_check":
        return {
            "automation_recommendation": "technical_code_spotcheck",
            "minimum_review_unit": "technical_code_family_samples",
            "operator_decision_hint": "Codes and standards can be valid labels, but need context checks.",
            "decision_options": [
                "accept_code_label",
                "append_context_to_code_label",
                "keep_needs_review",
            ],
        }
    if pattern_name.startswith("near_full_"):
        return {
            "automation_recommendation": "rule_tuning_candidate",
            "minimum_review_unit": "pattern_samples_only",
            "operator_decision_hint": "Usually residual wording cleanup; inspect samples before adding another rule.",
            "decision_options": [
                "add_rule_and_regenerate",
                "accept_machine_screened_pattern",
                "keep_needs_review",
            ],
        }
    return {
        "automation_recommendation": "manual_triage",
        "minimum_review_unit": "pattern_samples_only",
        "operator_decision_hint": "Inspect samples before choosing a rule-level handling.",
        "decision_options": [
            "add_rule_and_regenerate",
            "keep_needs_review",
        ],
    }


def build_ksa_short_label_pattern_report(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    limit: int = 100,
    sample_limit: int = 5,
) -> dict[str, Any]:
    """Group needs_review short-label rows into review patterns without writing statuses."""
    label_where, label_params = _label_scope(major_code)
    where_sql = "WHERE label.review_status = 'needs_review'"
    if label_where:
        scoped = label_where.strip()
        if scoped.upper().startswith("WHERE "):
            scoped = scoped[6:]
        where_sql += f"\n          AND ({scoped})"
    max_patterns = max(1, min(int(limit or 100), 1000))
    max_samples = max(1, min(int(sample_limit or 5), 20))
    rows = conn.execute(
        f"""
        SELECT
            label.label_id,
            label.concept_id,
            label.concept_type,
            oc.concept_name,
            label.source_ksa_id,
            label.source_atomic_id,
            label.source_scope_key,
            label.source_text,
            label.label_text,
            label.normalized_label_key,
            label.source_method,
            label.evidence_text,
            label.confidence_score,
            label.review_status
        FROM ontology_concept_label_candidates label
        JOIN ontology_concepts oc ON oc.concept_id = label.concept_id
        {where_sql}
        ORDER BY label.label_id
        """,
        label_params,
    ).fetchall()

    patterns: dict[str, dict[str, Any]] = {}
    quality_flag_counts: Counter[str] = Counter()
    concept_type_counts: Counter[str] = Counter()
    for row in rows:
        concept_type = str(row["concept_type"] or "")
        flags = ksa_label_quality_flags(
            row["source_text"] or "",
            row["label_text"] or "",
            concept_type,
        )
        pattern_name, recommended_handling = _label_pattern_for_review(
            row["source_text"],
            row["label_text"],
            concept_type,
            flags,
        )
        pattern_key = f"{concept_type}:{pattern_name}"
        source_scope = str(row["source_scope_key"] or "")
        major = source_scope.split(":", 1)[0] if source_scope else ""
        concept_type_counts[concept_type] += 1
        for flag in flags:
            quality_flag_counts[flag] += 1
        pattern = patterns.setdefault(
            pattern_key,
            {
                "pattern_key": pattern_key,
                "pattern_name": pattern_name,
                "concept_type": concept_type,
                "recommended_handling": recommended_handling,
                "row_count": 0,
                "concept_ids": set(),
                "label_families": Counter(),
                "label_family_concepts": {},
                "scope_keys": Counter(),
                "major_codes": Counter(),
                "quality_flag_counts": Counter(),
                "source_methods": Counter(),
                "samples": [],
            },
        )
        pattern["row_count"] += 1
        pattern["concept_ids"].add(int(row["concept_id"]))
        label_family_key = str(row["normalized_label_key"] or "")
        if label_family_key:
            pattern["label_families"][label_family_key] += 1
            pattern["label_family_concepts"].setdefault(label_family_key, set()).add(
                int(row["concept_id"])
            )
        if source_scope:
            pattern["scope_keys"][source_scope] += 1
        if major:
            pattern["major_codes"][major] += 1
        if row["source_method"]:
            pattern["source_methods"][str(row["source_method"])] += 1
        for flag in flags:
            pattern["quality_flag_counts"][flag] += 1
        if len(pattern["samples"]) < max_samples:
            sample = {
                "label_id": int(row["label_id"]),
                "concept_id": int(row["concept_id"]),
                "concept_name": row["concept_name"],
                "concept_type": concept_type,
                "source_text": _clip(row["source_text"], 240),
                "label_text": row["label_text"],
                "normalized_label_key": label_family_key,
                "source_scope_key": source_scope,
                "source_method": row["source_method"],
                "method_details": _label_method_details_from_evidence(row["evidence_text"]),
                "confidence_score": float(row["confidence_score"] or 0.0),
                "quality_flags": flags,
            }
            sample.update(
                _short_label_transform_metrics(row["source_text"], row["label_text"])
            )
            pattern["samples"].append(sample)

    pattern_rows: list[dict[str, Any]] = []
    for pattern in patterns.values():
        row_count = int(pattern["row_count"])
        quality_counts = dict(pattern["quality_flag_counts"])
        review_policy = _label_pattern_review_policy(str(pattern["pattern_name"]))
        label_family_concepts = pattern["label_family_concepts"]
        collision_counts = {
            key: len(concept_ids)
            for key, concept_ids in label_family_concepts.items()
            if len(concept_ids) > 1
        }
        for sample in pattern["samples"]:
            label_family_key = str(sample.get("normalized_label_key") or "")
            family_concept_count = len(label_family_concepts.get(label_family_key, set()))
            sample["label_family_pattern_row_count"] = int(
                pattern["label_families"].get(label_family_key, 0)
            )
            sample["label_family_pattern_concept_count"] = family_concept_count
            sample["collision_risk"] = (
                "pattern_label_collision" if family_concept_count > 1 else "none"
            )
        highest_flag_count = max(quality_counts.values()) if quality_counts else 0
        risk_score = min(100, 20 + min(50, row_count // 1000) + min(30, highest_flag_count // 1000))
        if pattern["pattern_name"] in {
            "generic_or_too_short_label",
            "short_acronym_context_check",
            "large_compression_check",
        }:
            risk_score = min(100, risk_score + 25)
        elif pattern["pattern_name"].startswith("near_full_"):
            risk_score = min(100, risk_score + 10)
        if risk_score >= 80:
            risk_level = "critical_pattern_review"
        elif risk_score >= 55:
            risk_level = "high_pattern_review"
        elif risk_score >= 30:
            risk_level = "medium_pattern_review"
        else:
            risk_level = "spotcheck_pattern_review"
        pattern_rows.append(
            {
                "pattern_key": pattern["pattern_key"],
                "pattern_name": pattern["pattern_name"],
                "concept_type": pattern["concept_type"],
                "row_count": row_count,
                "row_percent": round((row_count / len(rows)) * 100, 3) if rows else 0.0,
                "concept_count": len(pattern["concept_ids"]),
                "label_family_count": len(pattern["label_families"]),
                "collision_label_family_count": len(collision_counts),
                "max_collision_concept_count": max(collision_counts.values(), default=0),
                "collision_risk_hint": "label_family_collision_in_pattern"
                if collision_counts
                else "no_pattern_collision_detected",
                "scope_count": len(pattern["scope_keys"]),
                "major_count": len(pattern["major_codes"]),
                "quality_flag_counts": quality_counts,
                "source_method_counts": dict(pattern["source_methods"]),
                "top_label_families": _top_counter_items(pattern["label_families"], 8),
                "top_major_codes": _top_counter_items(pattern["major_codes"], 8),
                "top_scope_keys": _top_counter_items(pattern["scope_keys"], 8),
                "risk_score": risk_score,
                "risk_level": risk_level,
                "recommended_handling": pattern["recommended_handling"],
                "automation_recommendation": review_policy["automation_recommendation"],
                "minimum_review_unit": review_policy["minimum_review_unit"],
                "operator_decision_hint": review_policy["operator_decision_hint"],
                "decision_options": review_policy["decision_options"],
                "operator_review_scope": "pattern_level_sample_review_only",
                "samples": pattern["samples"],
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
        )
    pattern_rows.sort(
        key=lambda item: (
            -int(item.get("row_count") or 0),
            -int(item.get("risk_score") or 0),
            str(item.get("pattern_key") or ""),
        )
    )
    emitted = pattern_rows[:max_patterns]
    return {
        "schema": LABEL_PATTERN_REPORT_SCHEMA,
        "generated_at": _now_iso(),
        "ok": True,
        "status": "review_required",
        "scope": {
            "major_code": major_code,
            "limit": max_patterns,
            "sample_limit": max_samples,
        },
        "candidate_count": len(rows),
        "pattern_count": len(pattern_rows),
        "emitted_pattern_count": len(emitted),
        "estimated_first_pass_review_unit_count": len(emitted),
        "row_to_pattern_reduction_percent": round((1 - (len(pattern_rows) / len(rows))) * 100, 3)
        if rows
        else 0.0,
        "row_to_first_pass_reduction_percent": round((1 - (len(emitted) / len(rows))) * 100, 3)
        if rows
        else 0.0,
        "review_unit_model": "needs_review_rows_grouped_by_transform_pattern",
        "concept_type_counts": dict(concept_type_counts),
        "quality_flag_counts": dict(quality_flag_counts),
        "top_patterns": emitted,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "safety": {
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "raw_ksa_preserved": True,
            "human_reviewed_written_by_report": False,
            "accepted_written_by_report": False,
            "reviewed_written_by_report": False,
            "llm_reviewed_is_human_approval": False,
            "needs_review_is_not_failure": True,
        },
        "operator_guidance": [
            "Do not inspect all needs_review rows individually.",
            "Review the transform pattern and samples first, then decide whether the pattern needs rule tuning.",
            "This report is read-only; it does not approve or reject label candidates.",
            "near_full_* patterns are often wording normalization, while generic/short/acronym patterns need stricter review.",
        ],
    }


def build_ksa_short_label_family_report(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    limit: int = 100,
    sample_limit: int = 3,
) -> dict[str, Any]:
    """Build a first-pass, read-only family review queue for short KSA labels."""
    label_where, label_params = _label_scope(major_code)
    max_families = max(1, min(int(limit or 100), 1000))
    max_samples = max(1, min(int(sample_limit or 3), 20))
    trusted_status_sql = _trusted_label_review_status_placeholders()
    audited_label_review_exists_sql = _audited_label_review_exists_sql()
    rows = conn.execute(
        f"""
        SELECT
            label.label_id,
            label.concept_id,
            label.concept_type,
            oc.concept_name,
            label.source_ksa_id,
            label.source_atomic_id,
            label.source_scope_key,
            label.source_text,
            label.label_text,
            label.normalized_label_key,
            label.source_method,
            label.confidence_score,
            label.review_status,
            CASE
                WHEN label.review_status IN ({trusted_status_sql})
                 AND {audited_label_review_exists_sql}
                THEN 1
                ELSE 0
            END AS audited_trusted_review
        FROM ontology_concept_label_candidates label
        JOIN ontology_concepts oc ON oc.concept_id = label.concept_id
        {label_where}
        ORDER BY label.label_id
        """,
        (*TRUSTED_REVIEW_STATUSES, *AUTOMATED_REVIEWER_IDS, *label_params),
    ).fetchall()

    families: dict[str, dict[str, Any]] = {}
    status_counts: Counter[str] = Counter()
    concept_type_counts: Counter[str] = Counter()
    global_quality_flags: Counter[str] = Counter()
    for row in rows:
        concept_type = str(row["concept_type"] or "")
        normalized_key = str(row["normalized_label_key"] or row["label_text"] or "").strip()
        family_key = f"{concept_type}:{normalized_key}"
        source_scope = str(row["source_scope_key"] or "")
        major = source_scope.split(":", 1)[0] if source_scope else ""
        review_status = str(row["review_status"] or "")
        status_counts[review_status] += 1
        concept_type_counts[concept_type] += 1
        family = families.setdefault(
            family_key,
            {
                "family_key": family_key,
                "normalized_label_key": normalized_key,
                "concept_type": concept_type,
                "row_count": 0,
                "concept_ids": set(),
                "scope_keys": Counter(),
                "major_codes": Counter(),
                "label_texts": Counter(),
                "source_methods": Counter(),
                "review_status_counts": Counter(),
                "audited_trusted_review_count": 0,
                "unaudited_trusted_review_count": 0,
                "quality_flag_counts": Counter(),
                "missing_provenance_count": 0,
                "samples": [],
                "risk_samples": [],
            },
        )
        family["row_count"] += 1
        family["concept_ids"].add(int(row["concept_id"]))
        if source_scope:
            family["scope_keys"][source_scope] += 1
        if major:
            family["major_codes"][major] += 1
        if row["label_text"]:
            family["label_texts"][str(row["label_text"])] += 1
        if row["source_method"]:
            family["source_methods"][str(row["source_method"])] += 1
        family["review_status_counts"][review_status] += 1
        is_trusted_status = review_status in TRUSTED_REVIEW_STATUSES
        audited_trusted_review = bool(row["audited_trusted_review"])
        if is_trusted_status and audited_trusted_review:
            family["audited_trusted_review_count"] += 1
        elif is_trusted_status:
            family["unaudited_trusted_review_count"] += 1
        missing_provenance = row["source_ksa_id"] is None and row["source_atomic_id"] is None
        if missing_provenance:
            family["missing_provenance_count"] += 1
        flags = ksa_label_quality_flags(
            row["source_text"] or "",
            row["label_text"] or "",
            concept_type,
        )
        for flag in flags:
            family["quality_flag_counts"][flag] += 1
            global_quality_flags[flag] += 1
        sample = {
            "label_id": int(row["label_id"]),
            "concept_id": int(row["concept_id"]),
            "concept_name": row["concept_name"],
            "concept_type": concept_type,
            "label_text": row["label_text"],
            "source_text": _clip(row["source_text"], 220),
            "source_scope_key": source_scope,
            "source_method": row["source_method"],
            "confidence_score": float(row["confidence_score"] or 0.0),
            "review_status": review_status,
            "audited_trusted_review": audited_trusted_review,
            "human_approval_missing": is_trusted_status and not audited_trusted_review,
            "quality_flags": flags,
            "missing_provenance": missing_provenance,
        }
        if len(family["samples"]) < max_samples:
            family["samples"].append(sample)
        if (flags or missing_provenance or review_status != "llm_reviewed") and len(family["risk_samples"]) < max_samples:
            family["risk_samples"].append(sample)

    family_rows: list[dict[str, Any]] = []
    level_counts: Counter[str] = Counter()
    risk_family_count = 0
    for family in families.values():
        row_count = int(family["row_count"])
        concept_count = len(family["concept_ids"])
        scope_count = len(family["scope_keys"])
        major_count = len(family["major_codes"])
        review_status_counts = dict(family["review_status_counts"])
        quality_flag_counts = dict(family["quality_flag_counts"])
        score, reasons, level = _label_family_risk_score(
            normalized_label_key=str(family["normalized_label_key"] or ""),
            row_count=row_count,
            concept_count=concept_count,
            scope_count=scope_count,
            major_count=major_count,
            status_counts=review_status_counts,
            audited_trusted_review_count=int(family["audited_trusted_review_count"]),
            unaudited_trusted_review_count=int(family["unaudited_trusted_review_count"]),
            quality_flag_counts=quality_flag_counts,
            missing_provenance_count=int(family["missing_provenance_count"]),
            label_variant_count=len(family["label_texts"]),
        )
        level_counts[level] += 1
        if level != "machine_screened_spotcheck_only":
            risk_family_count += 1
        representative_label = family["label_texts"].most_common(1)[0][0] if family["label_texts"] else ""
        family_rows.append(
            {
                "family_key": family["family_key"],
                "normalized_label_key": family["normalized_label_key"],
                "representative_label": representative_label,
                "concept_type": family["concept_type"],
                "row_count": row_count,
                "row_percent": round((row_count / len(rows)) * 100, 3) if rows else 0.0,
                "concept_count": concept_count,
                "scope_count": scope_count,
                "major_count": major_count,
                "review_status_counts": review_status_counts,
                "audited_trusted_review_count": int(family["audited_trusted_review_count"]),
                "unaudited_trusted_review_count": int(family["unaudited_trusted_review_count"]),
                "source_method_counts": dict(family["source_methods"]),
                "quality_flag_counts": quality_flag_counts,
                "missing_provenance_count": int(family["missing_provenance_count"]),
                "label_variant_count": len(family["label_texts"]),
                "label_variants": _top_counter_items(family["label_texts"], 5),
                "top_major_codes": _top_counter_items(family["major_codes"], 5),
                "top_scope_keys": _top_counter_items(family["scope_keys"], 5),
                "risk_score": score,
                "risk_level": level,
                "risk_reasons": reasons,
                "operator_review_scope": "first_pass_family_sample_plus_risk_samples",
                "samples": family["samples"],
                "risk_samples": family["risk_samples"],
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
        )
    family_rows.sort(
        key=lambda item: (
            -int(item.get("risk_score") or 0),
            -int(item.get("row_count") or 0),
            str(item.get("family_key") or ""),
        )
    )
    emitted = family_rows[:max_families]
    first_pass_units = len(emitted) + len(global_quality_flags)
    return {
        "schema": LABEL_FAMILY_REPORT_SCHEMA,
        "generated_at": _now_iso(),
        "ok": True,
        "status": "review_required",
        "scope": {
            "major_code": major_code,
            "limit": max_families,
            "sample_limit": max_samples,
        },
        "candidate_count": len(rows),
        "label_family_count": len(family_rows),
        "risk_label_family_count": risk_family_count,
        "emitted_first_pass_family_count": len(emitted),
        "quality_flag_bucket_count": len(global_quality_flags),
        "estimated_first_pass_review_unit_count": first_pass_units,
        "row_to_family_reduction_percent": round((1 - (len(family_rows) / len(rows))) * 100, 3)
        if rows
        else 0.0,
        "row_to_first_pass_reduction_percent": round((1 - (first_pass_units / len(rows))) * 100, 3)
        if rows
        else 0.0,
        "review_unit_model": "top_risk_label_families_plus_quality_flag_buckets",
        "review_status_counts": dict(status_counts),
        "concept_type_counts": dict(concept_type_counts),
        "quality_flag_counts": dict(global_quality_flags),
        "risk_level_counts": dict(level_counts),
        "top_families": emitted,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "safety": {
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "raw_ksa_preserved": True,
            "human_reviewed_written_by_report": False,
            "accepted_written_by_report": False,
            "reviewed_written_by_report": False,
            "llm_reviewed_is_human_approval": False,
        },
        "operator_guidance": [
            "Do not click every short-label candidate row.",
            "Use this first-pass queue to inspect repeated or risky label families only.",
            "llm_reviewed means machine-screened label candidate, not human approval.",
            "A family decision is review guidance only; it does not write ontology_concept_label_candidates.review_status.",
            "Raw KSA text and ontology concept names are not changed by this report.",
        ],
    }


def write_ksa_short_label_family_report_markdown(report: dict[str, Any], out_path: Path) -> None:
    top_families = report.get("top_families") or []
    emitted_audited_trusted_count = sum(
        int(family.get("audited_trusted_review_count") or 0)
        for family in top_families
        if isinstance(family, dict)
    )
    emitted_unaudited_trusted_count = sum(
        int(family.get("unaudited_trusted_review_count") or 0)
        for family in top_families
        if isinstance(family, dict)
    )
    lines = [
        "# KSA Short Label Family Review Report",
        "",
        "This report groups short KSA label candidates for first-pass human review minimization.",
        "It is read-only and does not approve, promote, or write review statuses.",
        "",
        "## Summary",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- schema: `{report.get('schema')}`",
        f"- candidate_count: `{report.get('candidate_count')}`",
        f"- label_family_count: `{report.get('label_family_count')}`",
        f"- risk_label_family_count: `{report.get('risk_label_family_count')}`",
        f"- emitted_first_pass_family_count: `{report.get('emitted_first_pass_family_count')}`",
        f"- estimated_first_pass_review_unit_count: `{report.get('estimated_first_pass_review_unit_count')}`",
        f"- row_to_first_pass_reduction_percent: `{report.get('row_to_first_pass_reduction_percent')}`",
        f"- audited_trusted_review_count_in_emitted_families: `{emitted_audited_trusted_count}`",
        f"- unaudited_trusted_review_count_in_emitted_families: `{emitted_unaudited_trusted_count}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        "",
        "## Operator Guidance",
        "",
    ]
    for item in report.get("operator_guidance") or []:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## Review Status Counts",
            "",
            "| status | count |",
            "|---|---:|",
        ]
    )
    for status, count in sorted((report.get("review_status_counts") or {}).items()):
        lines.append(f"| {_md_cell(status)} | {count} |")
    lines.extend(
        [
            "",
            "## Quality Flags",
            "",
            "| flag | count |",
            "|---|---:|",
        ]
    )
    quality_counts = report.get("quality_flag_counts") or {}
    if quality_counts:
        for flag, count in sorted(quality_counts.items(), key=lambda item: (-int(item[1]), str(item[0]))):
            lines.append(f"| {_md_cell(flag)} | {count} |")
    else:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## First-Pass Label Families",
            "",
            "| label | type | rows | concepts | scopes | risk | status | audited trusted | unaudited trusted | sample source |",
            "|---|---|---:|---:|---:|---:|---|---:|---:|---|",
        ]
    )
    for family in top_families:
        samples = family.get("risk_samples") or family.get("samples") or []
        sample = samples[0] if samples else {}
        lines.append(
            "| "
            f"{_md_cell(family.get('representative_label'))} | "
            f"{_md_cell(family.get('concept_type'))} | "
            f"{family.get('row_count')} | "
            f"{family.get('concept_count')} | "
            f"{family.get('scope_count')} | "
            f"{family.get('risk_score')} | "
            f"{_md_cell(family.get('risk_level'))} | "
            f"{int(family.get('audited_trusted_review_count') or 0)} | "
            f"{int(family.get('unaudited_trusted_review_count') or 0)} | "
            f"{_md_cell(sample.get('source_text'))} |"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_ksa_short_label_family_report_csv(
    report: dict[str, Any],
    out_path: Path,
) -> dict[str, Any]:
    def _counts(value: Any) -> str:
        if not isinstance(value, dict) or not value:
            return ""
        return "; ".join(f"{key}={count}" for key, count in sorted(value.items()))

    def _list_counts(value: Any) -> str:
        if not isinstance(value, list):
            return ""
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("key"):
                parts.append(f"{item.get('key')}={item.get('count')}")
        return "; ".join(parts)

    def _sample_text(family: dict[str, Any], key: str) -> str:
        samples = family.get(key) if isinstance(family, dict) else None
        if not isinstance(samples, list) or not samples:
            return ""
        sample = samples[0] if isinstance(samples[0], dict) else {}
        return str(sample.get("source_text") or "")

    fieldnames = [
        "schema",
        "family_key",
        "representative_label",
        "concept_type",
        "row_count",
        "concept_count",
        "scope_count",
        "major_count",
        "risk_score",
        "risk_level",
        "risk_reasons",
        "review_status_counts",
        "audited_trusted_review_count",
        "unaudited_trusted_review_count",
        "quality_flag_counts",
        "source_method_counts",
        "top_major_codes",
        "operator_review_scope",
        "sample_source_text",
        "risk_sample_source_text",
        "decision",
        "reviewer_id",
        "reviewed_at",
        "rationale",
        "status_update_allowed",
        "db_writes",
        "approval_claim",
    ]
    rows: list[dict[str, Any]] = []
    for family in report.get("top_families") or []:
        rows.append(
            {
                "schema": LABEL_FAMILY_DECISION_SHEET_SCHEMA,
                "family_key": family.get("family_key") or "",
                "representative_label": family.get("representative_label") or "",
                "concept_type": family.get("concept_type") or "",
                "row_count": int(family.get("row_count") or 0),
                "concept_count": int(family.get("concept_count") or 0),
                "scope_count": int(family.get("scope_count") or 0),
                "major_count": int(family.get("major_count") or 0),
                "risk_score": int(family.get("risk_score") or 0),
                "risk_level": family.get("risk_level") or "",
                "risk_reasons": "; ".join(family.get("risk_reasons") or []),
                "review_status_counts": _counts(family.get("review_status_counts")),
                "audited_trusted_review_count": int(family.get("audited_trusted_review_count") or 0),
                "unaudited_trusted_review_count": int(family.get("unaudited_trusted_review_count") or 0),
                "quality_flag_counts": _counts(family.get("quality_flag_counts")),
                "source_method_counts": _counts(family.get("source_method_counts")),
                "top_major_codes": _list_counts(family.get("top_major_codes")),
                "operator_review_scope": family.get("operator_review_scope") or "",
                "sample_source_text": _sample_text(family, "samples"),
                "risk_sample_source_text": _sample_text(family, "risk_samples"),
                "decision": "",
                "reviewer_id": "",
                "reviewed_at": "",
                "rationale": "",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_safe_cell(row.get(key, "")) for key in fieldnames})
    return {
        "path": str(out_path),
        "record_count": len(rows),
        "schema": LABEL_FAMILY_DECISION_SHEET_SCHEMA,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
    }


def write_ksa_short_label_pattern_report_markdown(report: dict[str, Any], out_path: Path) -> None:
    lines = [
        "# KSA Short Label Pattern Review Report",
        "",
        "This read-only report groups `needs_review` short-label candidates by transform pattern.",
        "It is intended to reduce human review from row-by-row checking to pattern-level sample checking.",
        "",
        "## Summary",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- schema: `{report.get('schema')}`",
        f"- candidate_count: `{report.get('candidate_count')}`",
        f"- pattern_count: `{report.get('pattern_count')}`",
        f"- emitted_pattern_count: `{report.get('emitted_pattern_count')}`",
        f"- estimated_first_pass_review_unit_count: `{report.get('estimated_first_pass_review_unit_count')}`",
        f"- row_to_first_pass_reduction_percent: `{report.get('row_to_first_pass_reduction_percent')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        "",
        "## Operator Guidance",
        "",
    ]
    for item in report.get("operator_guidance") or []:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## Quality Flags",
            "",
            "| flag | count |",
            "|---|---:|",
        ]
    )
    quality_counts = report.get("quality_flag_counts") or {}
    if quality_counts:
        for flag, count in sorted(quality_counts.items(), key=lambda item: (-int(item[1]), str(item[0]))):
            lines.append(f"| {_md_cell(flag)} | {count} |")
    else:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## First-Pass Transform Patterns",
            "",
            "| pattern | type | rows | concepts | families | collision families | risk | handling | sample evidence |",
            "|---|---|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    for pattern in report.get("top_patterns") or []:
        samples = pattern.get("samples") or []
        sample = samples[0] if samples else {}
        sample_text = ""
        if sample:
            method_details = sample.get("method_details") or ""
            ratio = sample.get("short_label_length_ratio")
            removed = sample.get("short_label_removed_char_count")
            collision = sample.get("collision_risk") or ""
            evidence_bits = [
                f"{sample.get('source_text') or ''} -> {sample.get('label_text') or ''}",
                f"ratio={ratio}",
                f"removed={removed}",
                f"method={method_details}",
                f"collision={collision}",
            ]
            sample_text = "; ".join(str(bit) for bit in evidence_bits if str(bit).strip())
        lines.append(
            "| "
            f"{_md_cell(pattern.get('pattern_name'))} | "
            f"{_md_cell(pattern.get('concept_type'))} | "
            f"{pattern.get('row_count')} | "
            f"{pattern.get('concept_count')} | "
            f"{pattern.get('label_family_count')} | "
            f"{pattern.get('collision_label_family_count')} | "
            f"{pattern.get('risk_score')} | "
            f"{_md_cell(pattern.get('automation_recommendation'))}: "
            f"{_md_cell(pattern.get('minimum_review_unit'))}; "
            f"{_md_cell(pattern.get('recommended_handling'))} | "
            f"{_md_cell(sample_text)} |"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _csv_safe_cell(value: Any) -> Any:
    if isinstance(value, list):
        value = ";".join(str(item) for item in value)
    if not isinstance(value, str):
        return value
    if value[:1] in {"=", "+", "-", "@"} or value.lstrip()[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def write_ksa_short_label_pattern_report_csv(
    report: dict[str, Any],
    out_path: Path,
) -> dict[str, Any]:
    def _counts(value: Any) -> str:
        if not isinstance(value, dict) or not value:
            return ""
        return "; ".join(f"{key}={count}" for key, count in sorted(value.items()))

    def _list_counts(value: Any) -> str:
        if not isinstance(value, list):
            return ""
        return "; ".join(
            f"{item.get('key')}={item.get('count')}"
            for item in value
            if isinstance(item, dict) and item.get("key")
        )

    fieldnames = [
        "schema",
        "pattern_key",
        "pattern_name",
        "concept_type",
        "row_count",
        "concept_count",
        "label_family_count",
        "collision_label_family_count",
        "max_collision_concept_count",
        "collision_risk_hint",
        "scope_count",
        "major_count",
        "risk_score",
        "risk_level",
        "automation_recommendation",
        "minimum_review_unit",
        "operator_decision_hint",
        "decision_options",
        "recommended_handling",
        "quality_flag_counts",
        "source_method_counts",
        "top_label_families",
        "top_major_codes",
        "sample_source_text",
        "sample_label_text",
        "sample_method_details",
        "sample_removed_char_count",
        "sample_length_ratio",
        "sample_collision_risk",
        "sample_label_family_pattern_concept_count",
        "decision",
        "reviewer_id",
        "reviewed_at",
        "rationale",
        "status_update_allowed",
        "db_writes",
        "approval_claim",
    ]
    rows: list[dict[str, Any]] = []
    for pattern in report.get("top_patterns") or []:
        samples = pattern.get("samples") if isinstance(pattern, dict) else None
        sample = samples[0] if isinstance(samples, list) and samples and isinstance(samples[0], dict) else {}
        rows.append(
            {
                "schema": LABEL_PATTERN_DECISION_SHEET_SCHEMA,
                "pattern_key": pattern.get("pattern_key") or "",
                "pattern_name": pattern.get("pattern_name") or "",
                "concept_type": pattern.get("concept_type") or "",
                "row_count": int(pattern.get("row_count") or 0),
                "concept_count": int(pattern.get("concept_count") or 0),
                "label_family_count": int(pattern.get("label_family_count") or 0),
                "collision_label_family_count": int(
                    pattern.get("collision_label_family_count") or 0
                ),
                "max_collision_concept_count": int(
                    pattern.get("max_collision_concept_count") or 0
                ),
                "collision_risk_hint": pattern.get("collision_risk_hint") or "",
                "scope_count": int(pattern.get("scope_count") or 0),
                "major_count": int(pattern.get("major_count") or 0),
                "risk_score": int(pattern.get("risk_score") or 0),
                "risk_level": pattern.get("risk_level") or "",
                "automation_recommendation": pattern.get("automation_recommendation") or "",
                "minimum_review_unit": pattern.get("minimum_review_unit") or "",
                "operator_decision_hint": pattern.get("operator_decision_hint") or "",
                "decision_options": "; ".join(pattern.get("decision_options") or []),
                "recommended_handling": pattern.get("recommended_handling") or "",
                "quality_flag_counts": _counts(pattern.get("quality_flag_counts")),
                "source_method_counts": _counts(pattern.get("source_method_counts")),
                "top_label_families": _list_counts(pattern.get("top_label_families")),
                "top_major_codes": _list_counts(pattern.get("top_major_codes")),
                "sample_source_text": sample.get("source_text") or "",
                "sample_label_text": sample.get("label_text") or "",
                "sample_method_details": sample.get("method_details") or "",
                "sample_removed_char_count": sample.get("short_label_removed_char_count")
                if sample.get("short_label_removed_char_count") is not None
                else "",
                "sample_length_ratio": sample.get("short_label_length_ratio")
                if sample.get("short_label_length_ratio") is not None
                else "",
                "sample_collision_risk": sample.get("collision_risk") or "",
                "sample_label_family_pattern_concept_count": sample.get(
                    "label_family_pattern_concept_count"
                )
                if sample.get("label_family_pattern_concept_count") is not None
                else "",
                "decision": "",
                "reviewer_id": "",
                "reviewed_at": "",
                "rationale": "",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_safe_cell(row.get(key, "")) for key in fieldnames})
    return {
        "path": str(out_path),
        "record_count": len(rows),
        "schema": LABEL_PATTERN_DECISION_SHEET_SCHEMA,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
    }


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def write_ksa_label_candidate_report_markdown(report: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    counts = report.get("counts") or {}
    distributions = report.get("distributions") or {}
    quality = report.get("quality") or {}
    lines = [
        "# KSA Label Candidate Preprocessing Report",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- ok: `{report.get('ok')}`",
        f"- status: `{report.get('status')}`",
        f"- status_update_allowed: `{str(report.get('status_update_allowed')).lower()}`",
        f"- major_code: `{(report.get('scope') or {}).get('major_code')}`",
        "",
        "## Pipeline Contract",
        "",
        "| Stage | Table | Field | Contract |",
        "| --- | --- | --- | --- |",
    ]
    for item in report.get("pipeline_contract") or []:
        stage = _md_cell(item.get("stage"))
        if item.get("display_label"):
            stage = f"{stage}<br>{_md_cell(item.get('display_label'))}"
        lines.append(
            "| {stage} | `{table}` | `{field}` | {contract} |".format(
                stage=stage,
                table=_md_cell(item.get("table")),
                field=_md_cell(item.get("field")),
                contract=_md_cell(item.get("contract")),
            )
        )
    lines.extend(
        [
            "",
            "## Counts",
            "",
            "| Metric | Count |",
            "| --- | ---: |",
        ]
    )
    for key in sorted(counts):
        lines.append(f"| `{_md_cell(key)}` | {counts[key]} |")
    lines.extend(
        [
            "",
            "## Distributions",
            "",
            f"- by_source_method: `{json.dumps(distributions.get('by_source_method') or {}, ensure_ascii=False, sort_keys=True)}`",
            f"- by_concept_type: `{json.dumps(distributions.get('by_concept_type') or {}, ensure_ascii=False, sort_keys=True)}`",
            f"- by_review_status: `{json.dumps(distributions.get('by_review_status') or {}, ensure_ascii=False, sort_keys=True)}`",
            "",
            "## Collision Examples",
            "",
            "| Label | Concept Count | Row Count | Concept Types |",
            "| --- | ---: | ---: | --- |",
        ]
    )
    for row in quality.get("collision_examples") or []:
        lines.append(
            "| {label} | {concept_count} | {row_count} | {types} |".format(
                label=_md_cell(row.get("sample_label")),
                concept_count=int(row.get("concept_count") or 0),
                row_count=int(row.get("row_count") or 0),
                types=_md_cell(", ".join(row.get("concept_types") or [])),
            )
        )
    if not quality.get("collision_examples"):
        lines.append("| none | 0 | 0 |  |")
    lines.extend(
        [
            "",
            "## Generic Examples",
            "",
            "| Label | Concept Type | Concept Count | Row Count | Key |",
            "| --- | --- | ---: | ---: | --- |",
        ]
    )
    for row in quality.get("generic_examples") or []:
        lines.append(
            "| {label} | {concept_type} | {concept_count} | {row_count} | `{key}` |".format(
                label=_md_cell(row.get("label_text")),
                concept_type=_md_cell(row.get("concept_type")),
                concept_count=int(row.get("concept_count") or 0),
                row_count=int(row.get("row_count") or 0),
                key=_md_cell(row.get("normalized_label_key")),
            )
        )
    if not quality.get("generic_examples"):
        lines.append("| none |  | 0 | 0 |  |")
    lines.extend(
        [
            "",
            "## Quality Flag Counts",
            "",
            "| Flag | Count |",
            "| --- | ---: |",
        ]
    )
    quality_flag_counts = quality.get("quality_flag_counts") or {}
    for flag in sorted(quality_flag_counts):
        lines.append(f"| `{_md_cell(flag)}` | {quality_flag_counts[flag]} |")
    if not quality_flag_counts:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## Quality Flag Examples",
            "",
            "| Flag | Label | Source | Scope | Method |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    quality_flag_examples = quality.get("quality_flag_examples") or {}
    has_quality_examples = False
    for flag in sorted(quality_flag_examples):
        for row in quality_flag_examples.get(flag) or []:
            has_quality_examples = True
            lines.append(
                "| `{flag}` | {label} | {source} | `{scope}` | `{method}` |".format(
                    flag=_md_cell(flag),
                    label=_md_cell(row.get("label_text")),
                    source=_md_cell(row.get("source_text")),
                    scope=_md_cell(row.get("source_scope_key")),
                    method=_md_cell(row.get("source_method")),
                )
            )
    if not has_quality_examples:
        lines.append("| none |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Sample Source Provenance",
            "",
            "| Concept | Source Scope | Raw KSA | Atomic KSA | Source Text |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in report.get("samples") or []:
        lines.append(
            "| #{concept_id} {concept_type} `{concept_name}` | `{scope}` | {raw} | {atomic} | {source} |".format(
                concept_id=int(row.get("concept_id") or 0),
                concept_type=_md_cell(row.get("concept_type")),
                concept_name=_md_cell(row.get("concept_name")),
                scope=_md_cell(row.get("source_scope_key")),
                raw=_md_cell(row.get("raw_ksa_text")),
                atomic=_md_cell(row.get("atomic_ksa_text")),
                source=_md_cell(row.get("source_text")),
            )
        )
    if not report.get("samples"):
        lines.append("| none |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Sample Rows",
            "",
            "| Concept | Source Scope | Source Text | Short Label Candidate / 단어형 대표 라벨 후보 | Transform | Method | Status |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in report.get("samples") or []:
        lines.append(
            "| #{concept_id} {concept_type} `{concept_name}` | `{scope}` | {source} | {label} | `{transform}` | `{method}` | `{status}` |".format(
                concept_id=int(row.get("concept_id") or 0),
                concept_type=_md_cell(row.get("concept_type")),
                concept_name=_md_cell(row.get("concept_name")),
                scope=_md_cell(row.get("source_scope_key")),
                source=_md_cell(row.get("source_text")),
                label=_md_cell(row.get("short_label_candidate")),
                transform=_md_cell(
                    f"{row.get('short_label_transform_state')} "
                    f"{row.get('short_label_source_length')}->{row.get('short_label_label_length')} "
                    f"ratio={row.get('short_label_length_ratio')}"
                ),
                method=_md_cell(row.get("source_method")),
                status=_md_cell(row.get("review_status")),
            )
        )
    lines.extend(["", "## Notes", ""])
    for note in report.get("notes") or []:
        lines.append(f"- {_md_cell(note)}")
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


GENERIC_LABEL_KEYS = {
    "knowledge",
    "skill",
    "attitude",
    "management",
    "analysis",
    "planning",
    "operation",
    "support",
    "communication",
    "기술",
    "지식",
    "규격",
    "활용",
    "측정",
    "검사",
    "공유",
    "운영",
    "작성",
    "특성",
    "개념",
    "관리",
    "분석",
    "발표",
    "협상",
}


def build_ksa_label_review_seedpack(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Build rows that a human can inspect for label collisions and generic labels."""
    label_where, label_params = _label_scope(major_code)
    limit = max(1, min(int(limit or 500), 5000))
    generic_placeholders = ",".join("?" for _ in GENERIC_LABEL_KEYS)
    trusted_placeholders = _trusted_label_review_status_placeholders()
    trusted_filter = f"label.review_status NOT IN ({trusted_placeholders})"
    scoped_where = f"{label_where} AND {trusted_filter}" if label_where.strip() else f"WHERE {trusted_filter}"
    unbalanced_label_sql = _unbalanced_parentheses_sql("label.label_text")
    label_quality_sql = f"""
                label.label_text LIKE '%등'
                OR label.label_text LIKE '%및'
                OR {unbalanced_label_sql}
                OR (
                    LENGTH(TRIM(COALESCE(label.source_text, ''))) > 0
                    AND LENGTH(TRIM(label.label_text)) * 1.0 / LENGTH(TRIM(label.source_text)) < 0.15
                )
                OR (
                    label.source_method = 'rule_based_short_label_candidate'
                    AND LENGTH(TRIM(COALESCE(label.source_text, ''))) > 0
                    AND LENGTH(TRIM(label.label_text)) * 1.0 / LENGTH(TRIM(label.source_text)) >= 0.90
                )
    """
    label_priority_quality_sql = f"""
                label.label_text LIKE '%등'
                OR label.label_text LIKE '%및'
                OR {unbalanced_label_sql}
                OR (
                    LENGTH(TRIM(COALESCE(label.source_text, ''))) > 0
                    AND LENGTH(TRIM(label.label_text)) * 1.0 / LENGTH(TRIM(label.source_text)) < 0.15
                )
    """
    rows = conn.execute(
        f"""
        WITH scoped_labels AS (
            SELECT label.*
            FROM ontology_concept_label_candidates label
            {scoped_where}
        ),
        label_stats AS (
            SELECT normalized_label_key,
                   COUNT(*) AS row_count,
                   COUNT(DISTINCT concept_id) AS concept_count
            FROM scoped_labels
            GROUP BY normalized_label_key
        )
        SELECT
            label.label_id,
            label.concept_id,
            label.concept_type,
            oc.concept_name,
            label.source_ksa_id,
            label.source_atomic_id,
            source_ki.ksa_text_raw AS raw_ksa_text,
            source_atom.atom_text AS atomic_ksa_text,
            label.source_scope_key,
            label.source_text,
            label.label_text,
            label.normalized_label_key,
            label.source_method,
            label.confidence_score,
            label.review_status,
            stats.row_count AS collision_row_count,
            stats.concept_count AS collision_concept_count,
            CASE
                WHEN {label_quality_sql}
                THEN 'label_quality'
                WHEN stats.concept_count > 1 THEN 'collision'
                WHEN LENGTH(TRIM(label.label_text)) <= 3
                  OR label.normalized_label_key IN ({generic_placeholders})
                THEN 'generic'
                WHEN label.confidence_score < 0.55 THEN 'low_confidence'
                ELSE 'sample'
            END AS issue_type
        FROM scoped_labels label
        JOIN ontology_concepts oc ON oc.concept_id = label.concept_id
        LEFT JOIN ksa_atomic_items source_atom ON source_atom.atomic_id = label.source_atomic_id
        LEFT JOIN ksa_items source_ki ON source_ki.ksa_id = COALESCE(source_atom.ksa_id, label.source_ksa_id)
        JOIN label_stats stats ON stats.normalized_label_key = label.normalized_label_key
        WHERE stats.concept_count > 1
           OR LENGTH(TRIM(label.label_text)) <= 3
           OR label.normalized_label_key IN ({generic_placeholders})
           OR label.confidence_score < 0.55
           OR {label_quality_sql}
        ORDER BY
            CASE
                WHEN {label_priority_quality_sql}
                THEN 0
                WHEN stats.concept_count > 1 THEN 1
                WHEN LENGTH(TRIM(label.label_text)) <= 3
                  OR label.normalized_label_key IN ({generic_placeholders})
                THEN 2
                WHEN label.confidence_score < 0.55 THEN 3
                ELSE 4
            END,
            stats.concept_count DESC,
            stats.row_count DESC,
            label.normalized_label_key,
            label.label_id
        LIMIT ?
        """,
        (
            *label_params,
            *TRUSTED_REVIEW_STATUSES,
            *sorted(GENERIC_LABEL_KEYS),
            *sorted(GENERIC_LABEL_KEYS),
            *sorted(GENERIC_LABEL_KEYS),
            limit,
        ),
    ).fetchall()
    seed_rows = []
    for row in rows:
        quality_flags = ksa_label_quality_flags(
            row["source_text"] or "",
            row["label_text"] or "",
            row["concept_type"] or "",
        )
        issue_type = row["issue_type"]
        for flag in QUALITY_FLAG_PRIORITY:
            if flag in quality_flags:
                issue_type = flag
                break
        term_definition_evidence = _seedpack_term_definition_evidence(conn, row)
        task_evidence = _seedpack_task_evidence(conn, int(row["concept_id"]))
        seed_rows.append(
            {
                "issue_type": issue_type,
                "quality_flags": quality_flags,
                "review_prompt": _review_prompt_for_label_issue(issue_type, quality_flags, row),
                "review_focus": _review_focus_for_label_issue(issue_type, quality_flags),
                "allowed_decisions": list(KSA_LABEL_REVIEW_DECISIONS),
                "human_decision_required": True,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "trusted_status_write_allowed": False,
                "label_id": int(row["label_id"]),
                "concept_id": int(row["concept_id"]),
                "concept_type": row["concept_type"],
                "concept_name": row["concept_name"],
                "source_ksa_id": row["source_ksa_id"],
                "source_atomic_id": row["source_atomic_id"],
                **_source_text_payload(row),
                "source_scope_key": row["source_scope_key"],
                "source_text": row["source_text"],
                "label_text": row["label_text"],
                **_short_label_transform_metrics(row["source_text"], row["label_text"]),
                "normalized_label_key": row["normalized_label_key"],
                "source_method": row["source_method"],
                "confidence_score": float(row["confidence_score"]),
                "review_status": row["review_status"],
                "collision_row_count": int(row["collision_row_count"]),
                "collision_concept_count": int(row["collision_concept_count"]),
                **term_definition_evidence,
                **task_evidence,
                "raw_to_label_checked": "",
                "human_decision": "",
                "human_representative_label": "",
                "human_note": "",
            }
        )
    issue_counts: dict[str, int] = {}
    for row in seed_rows:
        issue_counts[row["issue_type"]] = issue_counts.get(row["issue_type"], 0) + 1
    return {
        "schema": "ksa_label_review_seedpack_v1",
        "generated_at": _now_iso(),
        "ok": True,
        "human_decision_required": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "scope": {"major_code": major_code, "limit": limit},
        "row_count": len(seed_rows),
        "issue_counts": issue_counts,
        "rows": seed_rows,
        "notes": [
            "Rows are review prompts only. They do not approve, reject, or update ontology label candidates.",
            "Fill human_decision and human_representative_label outside automation before any trusted status change.",
        ],
    }


def write_ksa_label_review_seedpack_jsonl(seedpack: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for row in seedpack.get("rows") or []:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_ksa_label_review_seedpack_csv(seedpack: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = seedpack.get("rows") or []
    fieldnames = [
        "issue_type",
        "quality_flags",
        "review_prompt",
        "review_focus",
        "allowed_decisions",
        "human_decision_required",
        "status_update_allowed",
        "db_writes",
        "approval_claim",
        "trusted_status_write_allowed",
        "label_id",
        "concept_id",
        "concept_type",
        "concept_name",
        "source_ksa_id",
        "source_atomic_id",
        "source_scope_key",
        "source_text",
        "raw_ksa_text",
        "atomic_ksa_text",
        "label_text",
        "short_label_transform_state",
        "short_label_source_length",
        "short_label_label_length",
        "short_label_removed_char_count",
        "short_label_length_ratio",
        "normalized_label_key",
        "source_method",
        "confidence_score",
        "review_status",
        "collision_row_count",
        "collision_concept_count",
        "term_definition_candidate",
        "term_definition_evidence",
        "term_definition_role",
        "term_definition_review_status",
        "term_definition_confidence",
        "task_evidence_count",
        "task_evidence_preview",
        "task_evidence_refs",
        "criteria_ids",
        "criteria_text_preview",
        "raw_to_label_checked",
        "human_decision",
        "human_representative_label",
        "human_note",
    ]
    def safe_cell(value: Any) -> Any:
        if isinstance(value, list):
            value = ";".join(str(item) for item in value)
        if not isinstance(value, str):
            return value
        if value[:1] in {"=", "+", "-", "@"} or value.lstrip()[:1] in {"=", "+", "-", "@"}:
            return "'" + value
        return value

    default_false_fields = {
        "human_decision_required",
        "status_update_allowed",
        "db_writes",
        "approval_claim",
        "trusted_status_write_allowed",
    }

    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: safe_cell(
                        row.get(key, False if key in default_false_fields else "")
                    )
                    for key in fieldnames
                }
            )


KSA_LABEL_AUTO_TRIAGE_BUCKETS = (
    "already_trusted_reviewed",
    "auto_pass_candidate",
    "revise_recommended",
    "human_sample_required",
    "domain_expert_required",
    "missing_label_gap",
)
KSA_LABEL_AUTO_TRIAGE_NON_DECISION_BUCKETS = {
    "already_trusted_reviewed",
    "missing_label_gap",
}
KSA_LABEL_AUTO_TRIAGE_BUCKET_PRIORITY = {
    "missing_label_gap": 0,
    "domain_expert_required": 1,
    "human_sample_required": 2,
    "revise_recommended": 3,
    "auto_pass_candidate": 4,
    "already_trusted_reviewed": 5,
}
KSA_LABEL_AUTO_TRIAGE_REVISE_FLAGS = {
    "empty_label",
    "unbalanced_parentheses",
    "dangling_enum_suffix",
    "residual_sentence_like_label",
    "overlong_word_label",
    "skill_suffix_stripped_to_generic",
    "changed_near_full_length",
}
KSA_LABEL_AUTO_TRIAGE_EXPERT_FLAGS = {
    "short_acronym_needs_context",
    "symbol_heavy",
    "digit_heavy",
}
KSA_LABEL_AUTO_TRIAGE_POLICY_CLASSIFICATION_SCHEMA = (
    "ksa_label_auto_triage_policy_classification_v1"
)
KSA_LABEL_AUTO_TRIAGE_POLICY_CLASSIFICATIONS = (
    "auto-pass-candidate",
    "modify-recommended",
    "human-sample-needed",
    "domain-expert-needed",
    "already-trusted-review",
    "missing-label-gap",
)
KSA_LABEL_AUTO_TRIAGE_POLICY_CLASSIFICATION_BY_BUCKET = {
    "auto_pass_candidate": "auto-pass-candidate",
    "revise_recommended": "modify-recommended",
    "human_sample_required": "human-sample-needed",
    "domain_expert_required": "domain-expert-needed",
    "already_trusted_reviewed": "already-trusted-review",
    "missing_label_gap": "missing-label-gap",
}
KSA_LABEL_AUTO_TRIAGE_POLICY_CLASSIFICATION_REASON = {
    "auto-pass-candidate": (
        "low-risk machine triage candidate; spot-check only and not human approval"
    ),
    "modify-recommended": (
        "label transformation or wording should be revised before row-level review"
    ),
    "human-sample-needed": (
        "candidate needs human sample review before any trusted status can be used"
    ),
    "domain-expert-needed": (
        "candidate contains domain-specific ambiguity that should be routed to a specialist"
    ),
    "already-trusted-review": (
        "row already has audited human-review provenance and is not an active review queue row"
    ),
    "missing-label-gap": (
        "source concept has evidence but no short-label candidate row yet; generate or inspect context first"
    ),
}


def _auto_triage_policy_classification(bucket: Any) -> str:
    return KSA_LABEL_AUTO_TRIAGE_POLICY_CLASSIFICATION_BY_BUCKET.get(
        str(bucket or ""),
        "human-sample-needed",
    )


def _auto_triage_policy_classification_payload(bucket: Any) -> dict[str, Any]:
    classification = _auto_triage_policy_classification(bucket)
    return {
        "classification_v2": classification,
        "classification_v2_schema": KSA_LABEL_AUTO_TRIAGE_POLICY_CLASSIFICATION_SCHEMA,
        "classification_reason": KSA_LABEL_AUTO_TRIAGE_POLICY_CLASSIFICATION_REASON[
            classification
        ],
        "requires_human_sample": classification == "human-sample-needed",
        "requires_domain_expert": classification == "domain-expert-needed",
        "classification_v2_is_decision_row": classification
        not in {"already-trusted-review", "missing-label-gap"},
    }


def _auto_triage_policy_classification_map() -> dict[str, dict[str, Any]]:
    return {
        bucket: _auto_triage_policy_classification_payload(bucket)
        for bucket in KSA_LABEL_AUTO_TRIAGE_BUCKETS
    }


def _auto_triage_policy_classification_counts(
    bucket_counts: Counter[str] | dict[str, int],
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for bucket, count in bucket_counts.items():
        counts[_auto_triage_policy_classification(bucket)] += int(count or 0)
    for classification in KSA_LABEL_AUTO_TRIAGE_POLICY_CLASSIFICATIONS:
        counts.setdefault(classification, 0)
    return {
        classification: int(counts[classification])
        for classification in KSA_LABEL_AUTO_TRIAGE_POLICY_CLASSIFICATIONS
    }


def _auto_triage_major_code_from_scope_key(scope_key: Any) -> str:
    text = str(scope_key or "").strip()
    if not text:
        return "unknown"
    return (text.split(":", 1)[0] or "unknown").zfill(2)


def _auto_triage_major_names(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = conn.execute(
            """
            SELECT major_code, MIN(major_name) AS major_name
            FROM classifications
            GROUP BY major_code
            """
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {str(row[0]).zfill(2): str(row[1] or "") for row in rows}


def _auto_triage_major_bucket_rollup(
    triage_rows: list[dict[str, Any]],
    *,
    major_names: dict[str, str],
) -> list[dict[str, Any]]:
    by_major: dict[str, Counter[str]] = {}
    for row in triage_rows:
        major_code = _auto_triage_major_code_from_scope_key(row.get("source_scope_key"))
        bucket = str(row.get("recommendation_bucket") or "")
        if not bucket:
            continue
        by_major.setdefault(major_code, Counter())[bucket] += 1
    rollup: list[dict[str, Any]] = []
    for major_code, counts in by_major.items():
        bucket_counts = {
            bucket: int(counts.get(bucket, 0)) for bucket in KSA_LABEL_AUTO_TRIAGE_BUCKETS
        }
        decision_row_count = sum(
            count
            for bucket, count in bucket_counts.items()
            if bucket not in KSA_LABEL_AUTO_TRIAGE_NON_DECISION_BUCKETS
            and bucket != "missing_label_gap"
        )
        manual_review_recommended_count = sum(
            count
            for bucket, count in bucket_counts.items()
            if bucket not in KSA_LABEL_AUTO_TRIAGE_NON_DECISION_BUCKETS
            and bucket not in {"auto_pass_candidate", "missing_label_gap"}
        )
        rollup.append(
            {
                "major_code": major_code,
                "major_name": major_names.get(major_code, ""),
                "row_count": int(sum(bucket_counts.values())),
                "decision_row_count": int(decision_row_count),
                "manual_review_recommended_count": int(manual_review_recommended_count),
                "auto_pass_candidate_count": int(bucket_counts.get("auto_pass_candidate", 0)),
                "already_trusted_reviewed_count": int(bucket_counts.get("already_trusted_reviewed", 0)),
                "bucket_counts": bucket_counts,
            }
        )
    return sorted(
        rollup,
        key=lambda item: (
            -int(item.get("manual_review_recommended_count") or 0),
            str(item.get("major_code") or ""),
        ),
    )


def _auto_triage_operator_strategy(
    *,
    bucket_counts: Counter[str],
    major_bucket_rollup: list[dict[str, Any]],
    decision_summary: dict[str, Any],
) -> dict[str, Any]:
    manual_review_count = int(
        decision_summary.get("full_scope_manual_review_recommended_count") or 0
    )
    auto_pass_count = int(decision_summary.get("full_scope_auto_pass_candidate_count") or 0)
    top_manual_majors = sorted(
        [
            {
                "major_code": str(item.get("major_code") or ""),
                "major_name": str(item.get("major_name") or ""),
                "manual_review_recommended_count": int(
                    item.get("manual_review_recommended_count") or 0
                ),
                "decision_row_count": int(item.get("decision_row_count") or 0),
                "domain_expert_required_count": int(
                    (item.get("bucket_counts") or {}).get("domain_expert_required") or 0
                ),
                "revise_recommended_count": int(
                    (item.get("bucket_counts") or {}).get("revise_recommended") or 0
                ),
            }
            for item in major_bucket_rollup
            if isinstance(item, dict)
        ],
        key=lambda item: (
            -item["manual_review_recommended_count"],
            item["major_code"],
        ),
    )[:5]
    return {
        "status": "review_required",
        "strategy": "triage_then_sample_review",
        "manual_review_recommended_count": manual_review_count,
        "auto_pass_candidate_count": auto_pass_count,
        "auto_pass_is_not_human_approval": True,
        "bulk_human_review_recommended": False,
        "recommended_sequence": [
            {
                "order": 1,
                "step": "rule_revision_first",
                "bucket": "revise_recommended",
                "row_count": int(bucket_counts.get("revise_recommended", 0)),
                "reason": "Fix transformation rules before asking a human to click rows.",
            },
            {
                "order": 2,
                "step": "domain_expert_sample",
                "bucket": "domain_expert_required",
                "row_count": int(bucket_counts.get("domain_expert_required", 0)),
                "reason": "Route acronym, symbol-heavy, and domain-specific labels to a specialist sample.",
            },
            {
                "order": 3,
                "step": "major_pattern_sample",
                "bucket": "human_sample_required",
                "row_count": int(bucket_counts.get("human_sample_required", 0)),
                "reason": "Sample by major, pattern, and label family instead of reviewing every row.",
            },
            {
                "order": 4,
                "step": "auto_pass_spotcheck_only",
                "bucket": "auto_pass_candidate",
                "row_count": auto_pass_count,
                "reason": "Treat clean HR-sample matches as spot-check candidates, not approvals.",
            },
        ],
        "top_manual_review_majors": top_manual_majors,
        "operator_notes": [
            "Do not bulk approve all llm_reviewed rows.",
            "Use major rollup to assign review batches, then use pattern/family reports to reduce row-level work.",
            "Only a filled human decision sheet can justify later trusted status updates.",
        ],
    }


def _auto_triage_classification_parts(
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    alias: str = "c",
) -> tuple[list[str], tuple[Any, ...]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("major_code", major_code),
        ("middle_code", middle_code),
        ("small_code", small_code),
        ("sub_code", sub_code),
    ):
        if value:
            clauses.append(f"{alias}.{column} = ?")
            params.append(value)
    return clauses, tuple(params)


def _auto_triage_scope_key_filter(
    label_alias: str,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> tuple[str, tuple[Any, ...]]:
    if not major_code:
        return "", ()
    codes = [major_code]
    if middle_code:
        codes.append(middle_code)
    if small_code:
        codes.append(small_code)
    if sub_code:
        codes.append(sub_code)
    scope_key = ":".join(codes)
    if len(codes) == 4:
        return f"{label_alias}.source_scope_key = ?", (scope_key,)
    return f"{label_alias}.source_scope_key LIKE ?", (f"{scope_key}:%",)


def _auto_triage_label_scope_condition(
    label_alias: str,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> tuple[str, tuple[Any, ...]]:
    if not any((major_code, middle_code, small_code, sub_code)):
        return "", ()
    if major_code and not any((middle_code, small_code, sub_code)):
        where_sql, params = _label_scope(major_code)
        condition = where_sql.strip()
        if condition.upper().startswith("WHERE "):
            condition = condition[6:].strip()
        return f"({condition})", params

    clauses, filter_params = _auto_triage_classification_parts(
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    class_filter = f" AND {' AND '.join(clauses)}" if clauses else ""
    source_conditions = [
        f"""
        EXISTS (
            SELECT 1
            FROM ksa_items scope_ki
            JOIN competency_elements scope_ce ON scope_ce.element_id = scope_ki.element_id
            JOIN competency_units scope_cu ON scope_cu.unit_code = scope_ce.unit_code
            JOIN classifications c ON c.classification_id = scope_cu.classification_id
            WHERE scope_ki.ksa_id = {label_alias}.source_ksa_id
              {class_filter}
        )
        """,
        f"""
        EXISTS (
            SELECT 1
            FROM ksa_atomic_items scope_atom
            JOIN competency_elements scope_ce ON scope_ce.element_id = scope_atom.element_id
            JOIN competency_units scope_cu ON scope_cu.unit_code = scope_ce.unit_code
            JOIN classifications c ON c.classification_id = scope_cu.classification_id
            WHERE scope_atom.atomic_id = {label_alias}.source_atomic_id
              {class_filter}
        )
        """,
    ]
    params: list[Any] = [*filter_params, *filter_params]
    scope_key_sql, scope_key_params = _auto_triage_scope_key_filter(
        label_alias,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    if scope_key_sql:
        source_conditions.append(scope_key_sql)
        params.extend(scope_key_params)
    return "(" + "\n        OR ".join(source_conditions) + ")", tuple(params)


def _auto_triage_label_scope_where(
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> tuple[str, tuple[Any, ...]]:
    condition, params = _auto_triage_label_scope_condition(
        "label",
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    return (f"WHERE {condition}", params) if condition else ("", ())


def _auto_triage_concept_scope_condition(
    concept_alias: str,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> tuple[str, tuple[Any, ...]]:
    if not any((major_code, middle_code, small_code, sub_code)):
        return "", ()
    clauses, filter_params = _auto_triage_classification_parts(
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    class_filter = f" AND {' AND '.join(clauses)}" if clauses else ""
    condition = f"""
        (
            EXISTS (
                SELECT 1
                FROM ksa_concept_links scope_kcl
                JOIN ksa_items scope_ki ON scope_ki.ksa_id = scope_kcl.ksa_id
                JOIN competency_elements scope_ce ON scope_ce.element_id = scope_ki.element_id
                JOIN competency_units scope_cu ON scope_cu.unit_code = scope_ce.unit_code
                JOIN classifications c ON c.classification_id = scope_cu.classification_id
                WHERE scope_kcl.concept_id = {concept_alias}.concept_id
                  {class_filter}
            )
            OR EXISTS (
                SELECT 1
                FROM ksa_atomic_concept_links scope_acl
                JOIN ksa_atomic_items scope_atom ON scope_atom.atomic_id = scope_acl.atomic_id
                JOIN competency_elements scope_ce ON scope_ce.element_id = scope_atom.element_id
                JOIN competency_units scope_cu ON scope_cu.unit_code = scope_ce.unit_code
                JOIN classifications c ON c.classification_id = scope_cu.classification_id
                WHERE scope_acl.concept_id = {concept_alias}.concept_id
                  {class_filter}
            )
            OR EXISTS (
                SELECT 1
                FROM criteria_concept_links scope_ccl
                JOIN performance_criteria scope_pc ON scope_pc.criteria_id = scope_ccl.criteria_id
                JOIN competency_elements scope_ce ON scope_ce.element_id = scope_pc.element_id
                JOIN competency_units scope_cu ON scope_cu.unit_code = scope_ce.unit_code
                JOIN classifications c ON c.classification_id = scope_cu.classification_id
                WHERE scope_ccl.concept_id = {concept_alias}.concept_id
                  {class_filter}
            )
        )
    """
    return condition, (*filter_params, *filter_params, *filter_params)


def _auto_triage_concept_source_evidence_condition(concept_alias: str) -> str:
    return f"""
        (
            EXISTS (
                SELECT 1
                FROM ksa_concept_links evidence_kcl
                WHERE evidence_kcl.concept_id = {concept_alias}.concept_id
            )
            OR EXISTS (
                SELECT 1
                FROM ksa_atomic_concept_links evidence_acl
                WHERE evidence_acl.concept_id = {concept_alias}.concept_id
            )
            OR EXISTS (
                SELECT 1
                FROM criteria_concept_links evidence_ccl
                WHERE evidence_ccl.concept_id = {concept_alias}.concept_id
            )
        )
    """


def _auto_triage_scope_codes_from_key(source_scope_key: Any) -> dict[str, str | None]:
    parts = str(source_scope_key or "").split(":")
    parts = [part.strip() or None for part in parts]
    while len(parts) < 4:
        parts.append(None)
    return {
        "major_code": parts[0],
        "middle_code": parts[1],
        "small_code": parts[2],
        "sub_code": parts[3],
    }


def _auto_triage_fetch_label_rows(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> list[sqlite3.Row]:
    where_sql, params = _auto_triage_label_scope_where(
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    trusted_status_sql = _trusted_label_review_status_placeholders()
    audited_review_sql = _audited_label_review_exists_sql()
    return conn.execute(
        f"""
        SELECT
            label.label_id,
            label.concept_id,
            label.concept_type,
            oc.concept_name,
            label.source_ksa_id,
            label.source_atomic_id,
            source_ki.ksa_text_raw AS raw_ksa_text,
            source_atom.atom_text AS atomic_ksa_text,
            label.source_scope_key,
            label.source_text,
            label.label_text,
            label.normalized_label_key,
            label.source_method,
            label.evidence_text,
            label.confidence_score,
            label.review_status,
            CASE
                WHEN label.review_status IN ({trusted_status_sql})
                 AND {audited_review_sql}
                THEN 1 ELSE 0
            END AS audited_trusted_review,
            c.major_code,
            c.middle_code,
            c.small_code,
            c.sub_code
        FROM ontology_concept_label_candidates label
        JOIN ontology_concepts oc ON oc.concept_id = label.concept_id
        LEFT JOIN ksa_atomic_items source_atom ON source_atom.atomic_id = label.source_atomic_id
        LEFT JOIN ksa_items source_ki ON source_ki.ksa_id = COALESCE(source_atom.ksa_id, label.source_ksa_id)
        LEFT JOIN competency_elements ce ON ce.element_id = COALESCE(source_atom.element_id, source_ki.element_id)
        LEFT JOIN competency_units cu ON cu.unit_code = ce.unit_code
        LEFT JOIN classifications c ON c.classification_id = cu.classification_id
        {where_sql}
        ORDER BY label.label_id
        """,
        (*TRUSTED_REVIEW_STATUSES, *AUTOMATED_REVIEWER_IDS, *params),
    ).fetchall()


def _auto_triage_pattern_key(row: sqlite3.Row | dict[str, Any], metrics: dict[str, Any]) -> str:
    return ":".join(
        [
            str(row["concept_type"] if isinstance(row, sqlite3.Row) else row.get("concept_type") or ""),
            str(row["source_method"] if isinstance(row, sqlite3.Row) else row.get("source_method") or ""),
            str(metrics.get("short_label_transform_state") or ""),
        ]
    )


def _auto_triage_build_label_stats(rows: list[sqlite3.Row]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        normalized_key = str(row["normalized_label_key"] or row["label_text"] or "").strip()
        scope_codes = _auto_triage_scope_codes_from_key(row["source_scope_key"])
        major_code = row["major_code"] or scope_codes.get("major_code") or ""
        stat = stats.setdefault(
            normalized_key,
            {
                "row_count": 0,
                "concept_ids": set(),
                "scope_keys": set(),
                "major_codes": set(),
            },
        )
        stat["row_count"] += 1
        stat["concept_ids"].add(int(row["concept_id"]))
        if row["source_scope_key"]:
            stat["scope_keys"].add(str(row["source_scope_key"]))
        if major_code:
            stat["major_codes"].add(str(major_code))
    return stats


def _auto_triage_build_trusted_sample_stats(rows: list[sqlite3.Row]) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    audited_status_counts: Counter[str] = Counter()
    quality_flag_counts: Counter[str] = Counter()
    clean_label_keys: Counter[str] = Counter()
    clean_patterns: Counter[str] = Counter()
    source_methods: Counter[str] = Counter()
    total_clean_row_count = 0
    clean_row_count = 0
    for row in rows:
        status_counts[str(row["review_status"] or "")] += 1
        audited_trusted_review = bool(row["audited_trusted_review"])
        if audited_trusted_review:
            audited_status_counts[str(row["review_status"] or "")] += 1
        if row["source_method"]:
            source_methods[str(row["source_method"])] += 1
        flags = ksa_label_quality_flags(
            row["source_text"] or "",
            row["label_text"] or "",
            row["concept_type"] or "",
        )
        for flag in flags:
            quality_flag_counts[flag] += 1
        if flags or not str(row["label_text"] or "").strip():
            continue
        total_clean_row_count += 1
        if not audited_trusted_review:
            continue
        clean_row_count += 1
        normalized_key = str(row["normalized_label_key"] or row["label_text"] or "").strip()
        if normalized_key:
            clean_label_keys[normalized_key] += 1
        metrics = _short_label_transform_metrics(row["source_text"], row["label_text"])
        clean_patterns[_auto_triage_pattern_key(row, metrics)] += 1
    return {
        "row_count": len(rows),
        "candidate_clean_row_count": total_clean_row_count,
        "clean_row_count": clean_row_count,
        "status_counts": status_counts,
        "audited_status_counts": audited_status_counts,
        "quality_flag_counts": quality_flag_counts,
        "clean_label_keys": clean_label_keys,
        "clean_patterns": clean_patterns,
        "source_methods": source_methods,
    }


def _auto_triage_hr_sample_support(
    row: sqlite3.Row,
    metrics: dict[str, Any],
    trusted_stats: dict[str, Any],
) -> dict[str, Any]:
    normalized_key = str(row["normalized_label_key"] or row["label_text"] or "").strip()
    pattern_key = _auto_triage_pattern_key(row, metrics)
    label_key_count = int((trusted_stats.get("clean_label_keys") or {}).get(normalized_key, 0))
    pattern_count = int((trusted_stats.get("clean_patterns") or {}).get(pattern_key, 0))
    if label_key_count and pattern_count:
        support = "label_key_and_pattern"
    elif label_key_count:
        support = "label_key"
    elif pattern_count:
        support = "pattern"
    else:
        support = "none"
    return {
        "hr_sample_support": support,
        "hr_sample_label_key_count": label_key_count,
        "hr_sample_pattern_count": pattern_count,
        "hr_sample_pattern_key": pattern_key,
    }


def _auto_triage_classify_label_row(
    row: sqlite3.Row,
    *,
    metrics: dict[str, Any],
    quality_flags: list[str],
    label_stats: dict[str, Any],
    trusted_stats: dict[str, Any],
) -> tuple[str, str, list[str], dict[str, Any]]:
    normalized_key = str(row["normalized_label_key"] or row["label_text"] or "").strip()
    stat = label_stats.get(normalized_key) or {}
    concept_count = len(stat.get("concept_ids") or [])
    scope_count = len(stat.get("scope_keys") or [])
    major_count = len(stat.get("major_codes") or [])
    support = _auto_triage_hr_sample_support(row, metrics, trusted_stats)
    flags = set(quality_flags)
    confidence = float(row["confidence_score"] or 0.0)
    rationale: list[str] = []
    review_status = str(row["review_status"] or "")
    audited_trusted_review = bool(row["audited_trusted_review"])

    if review_status in TRUSTED_REVIEW_STATUSES:
        if audited_trusted_review:
            return (
                "already_trusted_reviewed",
                "audited_trusted_status_already_reviewed",
                [
                    "row already has trusted human-review audit evidence",
                    "not included in auto-pass candidate queue",
                ],
                support,
            )
        return (
            "human_sample_required",
            "trusted_status_missing_audit",
            [
                "trusted review_status is present but required human audit provenance is missing",
                "treat as not human-approved until audit evidence is reconciled",
            ],
            support,
        )

    if review_status == "needs_review":
        return (
            "human_sample_required",
            "existing_needs_review_status",
            ["candidate is already marked needs_review"],
            support,
        )
    if review_status == "rejected":
        return (
            "human_sample_required",
            "existing_rejected_status",
            ["candidate is already marked rejected; do not re-surface as auto-pass"],
            support,
        )

    if not str(row["label_text"] or "").strip():
        return (
            "missing_label_gap",
            "empty_label_text",
            ["label_text is empty; no recommendation label is available"],
            support,
        )
    if not str(row["source_text"] or "").strip():
        return (
            "revise_recommended",
            "missing_source_text",
            ["source_text is empty, so source-to-label equivalence cannot be checked"],
            support,
        )
    if flags & KSA_LABEL_AUTO_TRIAGE_REVISE_FLAGS:
        revise_flags = sorted(flags & KSA_LABEL_AUTO_TRIAGE_REVISE_FLAGS)
        return (
            "revise_recommended",
            "quality_flags_require_revision",
            [f"quality flag: {flag}" for flag in revise_flags],
            support,
        )
    if flags & KSA_LABEL_AUTO_TRIAGE_EXPERT_FLAGS:
        expert_flags = sorted(flags & KSA_LABEL_AUTO_TRIAGE_EXPERT_FLAGS)
        return (
            "domain_expert_required",
            "domain_specificity_check",
            [f"quality flag: {flag}" for flag in expert_flags],
            support,
        )
    if "generic_or_low_specificity" in flags:
        return (
            "human_sample_required",
            "generic_specificity_sample_check",
            ["quality flag: generic_or_low_specificity"],
            support,
        )
    if "very_low_label_source_ratio" in flags:
        if normalized_key in GENERIC_LABEL_KEYS or len(normalized_key) <= 3:
            return (
                "human_sample_required",
                "large_compression_generic_sample_check",
                ["quality flag: very_low_label_source_ratio with broad or short label"],
                support,
            )
        return (
            "revise_recommended",
            "large_compression_revise",
            ["quality flag: very_low_label_source_ratio"],
            support,
        )
    if normalized_key in GENERIC_LABEL_KEYS or len(normalized_key) <= 3:
        return (
            "human_sample_required",
            "generic_label_key",
            ["normalized label key is too broad for automatic acceptance"],
            support,
        )
    if concept_count >= 5 or major_count >= 3:
        return (
            "domain_expert_required",
            "broad_label_collision",
            [
                f"same label key spans {concept_count} concepts",
                f"same label key spans {major_count} major scopes",
            ],
            support,
        )
    if concept_count > 1:
        return (
            "human_sample_required",
            "label_collision_sample_check",
            [f"same label key maps to {concept_count} concepts in this scope"],
            support,
        )
    if row["source_ksa_id"] is None and row["source_atomic_id"] is None:
        return (
            "human_sample_required",
            "missing_source_provenance",
            ["candidate lacks source_ksa_id and source_atomic_id provenance"],
            support,
        )
    if confidence < 0.55:
        return (
            "revise_recommended",
            "low_confidence_candidate",
            [f"confidence_score={confidence:.3f}"],
            support,
        )
    if confidence < 0.70:
        return (
            "human_sample_required",
            "medium_confidence_sample_check",
            [f"confidence_score={confidence:.3f}"],
            support,
        )
    if support["hr_sample_support"] in {"label_key", "label_key_and_pattern"}:
        rationale.append(f"HR sample support: {support['hr_sample_support']}")
        rationale.append(f"confidence_score={confidence:.3f}")
        return "auto_pass_candidate", "clean_hr_sample_supported", rationale, support
    if support["hr_sample_support"] == "pattern":
        return (
            "human_sample_required",
            "pattern_only_sample_check",
            [
                "candidate only matches the trusted sample transform pattern; label-key support is required before auto-pass"
            ],
            support,
        )
    return (
        "human_sample_required",
        "no_hr_sample_support",
        ["candidate is clean but not covered by the trusted HR sample scope"],
        support,
    )


def _auto_triage_count_scoped_concepts(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> int:
    concept_condition, params = _auto_triage_concept_scope_condition(
        "oc",
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    where_parts = ["oc.concept_type IN ('knowledge', 'skill', 'attitude')"]
    if concept_condition:
        where_parts.append(concept_condition)
    return _scalar(
        conn,
        f"""
        SELECT COUNT(*)
        FROM ontology_concepts oc
        WHERE {" AND ".join(where_parts)}
        """,
        params,
    )


def _auto_triage_missing_label_gap_where(
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    require_source_evidence: bool = True,
    require_no_source_evidence: bool = False,
) -> tuple[str, tuple[Any, ...]]:
    concept_condition, concept_params = _auto_triage_concept_scope_condition(
        "oc",
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    label_condition, label_params = _auto_triage_label_scope_condition(
        "label",
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    where_parts = ["oc.concept_type IN ('knowledge', 'skill', 'attitude')"]
    if concept_condition:
        where_parts.append(concept_condition)
    source_evidence_condition = _auto_triage_concept_source_evidence_condition("oc")
    if require_source_evidence:
        where_parts.append(source_evidence_condition)
    elif require_no_source_evidence:
        where_parts.append(f"NOT {source_evidence_condition}")
    label_scope_sql = f" AND {label_condition}" if label_condition else ""
    where_parts.append(
        f"""
        NOT EXISTS (
            SELECT 1
            FROM ontology_concept_label_candidates label
            WHERE label.concept_id = oc.concept_id
              {label_scope_sql}
        )
        """
    )
    return "WHERE " + " AND ".join(where_parts), (*concept_params, *label_params)


def _auto_triage_missing_label_gap_count(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> int:
    where_sql, params = _auto_triage_missing_label_gap_where(
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    return _scalar(conn, f"SELECT COUNT(*) FROM ontology_concepts oc {where_sql}", params)


def _auto_triage_orphan_missing_label_gap_count(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> int:
    where_sql, params = _auto_triage_missing_label_gap_where(
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        require_source_evidence=False,
        require_no_source_evidence=True,
    )
    return _scalar(conn, f"SELECT COUNT(*) FROM ontology_concepts oc {where_sql}", params)


def _auto_triage_missing_label_gap_samples(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    where_sql, params = _auto_triage_missing_label_gap_where(
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    rows = conn.execute(
        f"""
        SELECT
            oc.concept_id,
            oc.concept_name,
            oc.concept_type,
            oc.definition_status,
            oc.review_status
        FROM ontology_concepts oc
        {where_sql}
        ORDER BY oc.concept_type, oc.concept_name, oc.concept_id
        LIMIT ?
        """,
        (*params, max(1, int(limit or 20))),
    ).fetchall()
    samples: list[dict[str, Any]] = []
    for row in rows:
        samples.append(
            {
                "recommendation_bucket": "missing_label_gap",
                "recommendation_rule": "reviewable_no_label_candidate_in_scope",
                "recommendation_rationale": [
                    "concept has source evidence but no short-label candidate row"
                ],
                "label_id": None,
                "concept_id": int(row["concept_id"]),
                "concept_type": row["concept_type"],
                "concept_name": row["concept_name"],
                "concept_definition_status": row["definition_status"],
                "concept_review_status": row["review_status"],
                "source_scope_key": "",
                "source_text": "",
                "label_text": "",
                "normalized_label_key": "",
                "source_method": "",
                "confidence_score": None,
                "review_status": "",
                "quality_flags": [],
                "hr_sample_support": "none",
                "hr_sample_label_key_count": 0,
                "hr_sample_pattern_count": 0,
                "hr_sample_pattern_key": "",
                "collision_row_count": 0,
                "collision_concept_count": 0,
                "collision_scope_count": 0,
                "collision_major_count": 0,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_reviewed_written_by_report": False,
                **_auto_triage_policy_classification_payload("missing_label_gap"),
            }
        )
    return samples


def _missing_label_gap_review_rows(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    where_sql, params = _auto_triage_missing_label_gap_where(
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    max_rows = max(1, min(int(limit or 200), 10000))
    rows = conn.execute(
        f"""
        SELECT
            oc.concept_id,
            oc.concept_name,
            oc.normalized_key,
            oc.concept_type,
            oc.definition_status,
            oc.review_status,
            COUNT(DISTINCT kcl.ksa_id) AS ksa_link_count,
            COUNT(DISTINCT kac.atomic_id) AS atomic_link_count,
            COUNT(DISTINCT ccl.criteria_id) AS criteria_link_count,
            MIN(c.major_code) AS major_code,
            MIN(c.major_name) AS major_name,
            MIN(c.middle_code) AS middle_code,
            MIN(c.middle_name) AS middle_name,
            MIN(c.small_code) AS small_code,
            MIN(c.small_name) AS small_name,
            MIN(c.sub_code) AS sub_code,
            MIN(c.sub_name) AS sub_name,
            MIN(cu.unit_code) AS sample_unit_code,
            MIN(cu.unit_name_raw) AS sample_unit_name,
            MIN(ce.element_name_raw) AS sample_element_name,
            MIN(ki.ksa_text_raw) AS sample_ksa_text,
            MIN(kai.atom_text) AS sample_atomic_text,
            MIN(pc.criteria_text_raw) AS sample_criteria_text
        FROM ontology_concepts oc
        LEFT JOIN ksa_concept_links kcl ON kcl.concept_id = oc.concept_id
        LEFT JOIN ksa_items ki ON ki.ksa_id = kcl.ksa_id
        LEFT JOIN competency_elements ce ON ce.element_id = ki.element_id
        LEFT JOIN competency_units cu ON cu.unit_code = ce.unit_code
        LEFT JOIN classifications c ON c.classification_id = cu.classification_id
        LEFT JOIN ksa_atomic_concept_links kac ON kac.concept_id = oc.concept_id
        LEFT JOIN ksa_atomic_items kai ON kai.atomic_id = kac.atomic_id
        LEFT JOIN criteria_concept_links ccl ON ccl.concept_id = oc.concept_id
        LEFT JOIN performance_criteria pc ON pc.criteria_id = ccl.criteria_id
        {where_sql}
        GROUP BY
            oc.concept_id,
            oc.concept_name,
            oc.normalized_key,
            oc.concept_type,
            oc.definition_status,
            oc.review_status
        ORDER BY
            oc.concept_type,
            oc.concept_name,
            oc.concept_id
        LIMIT ?
        """,
        (*params, max_rows),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        source_scope_key = ":".join(
            str(row[column] or "")
            for column in ("major_code", "middle_code", "small_code", "sub_code")
        ).strip(":")
        result.append(
            {
                "sequence": index,
                "recommendation_bucket": "missing_label_gap",
                "recommendation_rule": "reviewable_label_candidate_generation_required",
                "concept_id": int(row["concept_id"]),
                "concept_type": row["concept_type"],
                "concept_name": row["concept_name"],
                "normalized_key": row["normalized_key"],
                "definition_status": row["definition_status"],
                "review_status": row["review_status"],
                "major_code": row["major_code"],
                "major_name": row["major_name"],
                "middle_code": row["middle_code"],
                "middle_name": row["middle_name"],
                "small_code": row["small_code"],
                "small_name": row["small_name"],
                "sub_code": row["sub_code"],
                "sub_name": row["sub_name"],
                "source_scope_key": source_scope_key,
                "sample_unit_code": row["sample_unit_code"],
                "sample_unit_name": row["sample_unit_name"],
                "sample_element_name": row["sample_element_name"],
                "sample_ksa_text": _clip(row["sample_ksa_text"], 260),
                "sample_atomic_text": _clip(row["sample_atomic_text"], 260),
                "sample_criteria_text": _clip(row["sample_criteria_text"], 260),
                "ksa_link_count": int(row["ksa_link_count"] or 0),
                "atomic_link_count": int(row["atomic_link_count"] or 0),
                "criteria_link_count": int(row["criteria_link_count"] or 0),
                "operator_action": "create_or_review_short_label_candidate_from_source_evidence",
                "is_button_review_row": False,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_reviewed_written_by_report": False,
            }
        )
    return result


def build_ksa_missing_label_gap_review_pack(
    conn: sqlite3.Connection,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    limit: int = 200,
    sample_limit: int = 10,
) -> dict[str, Any]:
    """Build a read-only operator pack for linked concepts that have no label candidate."""
    max_rows = max(1, min(int(limit or 200), 10000))
    max_samples = max(1, min(int(sample_limit or 10), 50))
    total_gap_count = _auto_triage_missing_label_gap_count(
        conn,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    orphan_backlog_count = _auto_triage_orphan_missing_label_gap_count(
        conn,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    rows = _missing_label_gap_review_rows(
        conn,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        limit=max_rows,
    )
    concept_type_counts: Counter[str] = Counter()
    definition_status_counts: Counter[str] = Counter()
    review_status_counts: Counter[str] = Counter()
    scope_counts: Counter[str] = Counter()
    for row in rows:
        concept_type_counts[str(row.get("concept_type") or "")] += 1
        definition_status_counts[str(row.get("definition_status") or "")] += 1
        review_status_counts[str(row.get("review_status") or "")] += 1
        if row.get("source_scope_key"):
            scope_counts[str(row.get("source_scope_key"))] += 1
    return {
        "schema": LABEL_MISSING_GAP_REVIEW_PACK_SCHEMA,
        "generated_at": _now_iso(),
        "ok": True,
        "status": "review_required",
        "scope": {
            "major_code": major_code,
            "middle_code": middle_code,
            "small_code": small_code,
            "sub_code": sub_code,
            "limit": max_rows,
            "sample_limit": max_samples,
        },
        "counts": {
            "missing_label_gap": total_gap_count,
            "reviewable_missing_label_gap": total_gap_count,
            "orphan_raw_concept_backlog": orphan_backlog_count,
            "emitted_rows": len(rows),
            "csv_rows": len(rows),
            "sample_rows": min(len(rows), max_samples),
            "remaining_after_emit": max(total_gap_count - len(rows), 0),
        },
        "missing_label_gap_count": total_gap_count,
        "reviewable_missing_label_gap_count": total_gap_count,
        "orphan_raw_concept_backlog_count": orphan_backlog_count,
        "emitted_row_count": len(rows),
        "output_limit_applied": len(rows) < total_gap_count,
        "concept_type_counts": dict(concept_type_counts),
        "definition_status_counts": dict(definition_status_counts),
        "review_status_counts": dict(review_status_counts),
        "top_source_scopes": _top_counter_items(scope_counts, 20),
        "rows": rows,
        "samples": rows[:max_samples],
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_reviewed_written_by_report": False,
        "safety": {
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "human_reviewed_written_by_report": False,
            "accepted_written_by_report": False,
            "reviewed_written_by_report": False,
            "missing_label_gap_is_not_button_review_row": True,
            "orphan_raw_concepts_excluded_from_review_rows": True,
        },
        "operator_guidance": [
            "This pack lists linked ontology concepts that have source evidence but no short-label candidate row.",
            "Orphan raw concepts without KSA, atomic KSA, or criteria links are counted separately and excluded from review rows.",
            "Rows are not approval rows and do not write review statuses.",
            "Use this pack to generate or review candidate labels before human approval.",
            "Do not treat missing_label_gap counts as human review button rows.",
        ],
    }


def build_ksa_label_auto_triage_report(
    conn: sqlite3.Connection,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    trusted_major_code: str | None = "02",
    trusted_middle_code: str | None = "02",
    trusted_small_code: str | None = "02",
    limit: int = 200,
    sample_limit: int = 5,
) -> dict[str, Any]:
    """Build a read-only HR-sample-based auto-triage report for KSA short labels."""
    max_rows = max(1, min(int(limit or 200), 5000))
    max_samples = max(1, min(int(sample_limit or 5), 50))
    target_rows = _auto_triage_fetch_label_rows(
        conn,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    trusted_rows = _auto_triage_fetch_label_rows(
        conn,
        major_code=trusted_major_code,
        middle_code=trusted_middle_code,
        small_code=trusted_small_code,
    )
    label_stats = _auto_triage_build_label_stats(target_rows)
    trusted_stats = _auto_triage_build_trusted_sample_stats(trusted_rows)
    status_counts: Counter[str] = Counter()
    bucket_counts: Counter[str] = Counter()
    rule_counts: Counter[str] = Counter()
    quality_flag_counts: Counter[str] = Counter()
    source_method_counts: Counter[str] = Counter()
    target_scope_counts: Counter[str] = Counter()
    triage_rows: list[dict[str, Any]] = []

    for row in target_rows:
        status_counts[str(row["review_status"] or "")] += 1
        if row["source_method"]:
            source_method_counts[str(row["source_method"])] += 1
        if row["source_scope_key"]:
            target_scope_counts[str(row["source_scope_key"])] += 1
        quality_flags = ksa_label_quality_flags(
            row["source_text"] or "",
            row["label_text"] or "",
            row["concept_type"] or "",
        )
        for flag in quality_flags:
            quality_flag_counts[flag] += 1
        metrics = _short_label_transform_metrics(row["source_text"], row["label_text"])
        normalized_key = str(row["normalized_label_key"] or row["label_text"] or "").strip()
        stat = label_stats.get(normalized_key) or {}
        bucket, rule, rationale, support = _auto_triage_classify_label_row(
            row,
            metrics=metrics,
            quality_flags=quality_flags,
            label_stats=label_stats,
            trusted_stats=trusted_stats,
        )
        bucket_counts[bucket] += 1
        rule_counts[rule] += 1
        audited_trusted_review = bool(row["audited_trusted_review"])
        triage_rows.append(
            {
                "recommendation_bucket": bucket,
                **_auto_triage_policy_classification_payload(bucket),
                "recommendation_rule": rule,
                "recommendation_rationale": rationale,
                "label_id": int(row["label_id"]),
                "concept_id": int(row["concept_id"]),
                "concept_type": row["concept_type"],
                "concept_name": row["concept_name"],
                "source_ksa_id": row["source_ksa_id"],
                "source_atomic_id": row["source_atomic_id"],
                **_source_text_payload(row),
                "source_scope_key": row["source_scope_key"],
                "source_text": _clip(row["source_text"], 260),
                "label_text": row["label_text"],
                "normalized_label_key": normalized_key,
                "source_method": row["source_method"],
                "method_details": _label_method_details_from_evidence(row["evidence_text"]),
                "confidence_score": float(row["confidence_score"] or 0.0),
                "review_status": row["review_status"],
                "audited_trusted_review": audited_trusted_review,
                "human_approval_missing": not audited_trusted_review,
                "recommendation_is_human_approval": False,
                "candidate_bucket_is_not_approval": True,
                "operator_trust_state": "audited_human_reviewed"
                if audited_trusted_review
                else "not_human_approved",
                "quality_flags": quality_flags,
                **metrics,
                **support,
                "collision_row_count": int(stat.get("row_count") or 0),
                "collision_concept_count": len(stat.get("concept_ids") or []),
                "collision_scope_count": len(stat.get("scope_keys") or []),
                "collision_major_count": len(stat.get("major_codes") or []),
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_reviewed_written_by_report": False,
            }
        )

    missing_gap_count = _auto_triage_missing_label_gap_count(
        conn,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    orphan_missing_gap_count = _auto_triage_orphan_missing_label_gap_count(
        conn,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    if missing_gap_count:
        bucket_counts["missing_label_gap"] += missing_gap_count
        rule_counts["reviewable_no_label_candidate_in_scope"] += missing_gap_count
        triage_rows.extend(
            _auto_triage_missing_label_gap_samples(
                conn,
                major_code=major_code,
                middle_code=middle_code,
                small_code=small_code,
                sub_code=sub_code,
                limit=max(max_samples, min(max_rows, 50)),
            )
        )

    for bucket in KSA_LABEL_AUTO_TRIAGE_BUCKETS:
        bucket_counts.setdefault(bucket, 0)

    triage_rows.sort(
        key=lambda item: (
            KSA_LABEL_AUTO_TRIAGE_BUCKET_PRIORITY.get(
                str(item.get("recommendation_bucket") or ""),
                99,
            ),
            str(item.get("recommendation_rule") or ""),
            -float(item.get("confidence_score") or 0.0),
            int(item.get("label_id") or 0),
            int(item.get("concept_id") or 0),
        )
    )
    samples_by_bucket: dict[str, list[dict[str, Any]]] = {
        bucket: [] for bucket in KSA_LABEL_AUTO_TRIAGE_BUCKETS
    }
    for row in triage_rows:
        bucket = str(row.get("recommendation_bucket") or "")
        if bucket in samples_by_bucket and len(samples_by_bucket[bucket]) < max_samples:
            samples_by_bucket[bucket].append(row)

    concept_scope_count = _auto_triage_count_scoped_concepts(
        conn,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    emitted_rows = triage_rows[:max_rows]
    major_bucket_rollup = _auto_triage_major_bucket_rollup(
        triage_rows,
        major_names=_auto_triage_major_names(conn),
    )
    decision_rows = [
        row
        for row in emitted_rows
        if row.get("recommendation_bucket") not in KSA_LABEL_AUTO_TRIAGE_NON_DECISION_BUCKETS
    ]
    already_trusted_emitted_rows = [
        row
        for row in emitted_rows
        if row.get("recommendation_bucket") == "already_trusted_reviewed"
    ]
    decision_bucket_names = [
        bucket
        for bucket in KSA_LABEL_AUTO_TRIAGE_BUCKETS
        if bucket not in KSA_LABEL_AUTO_TRIAGE_NON_DECISION_BUCKETS
        and bucket != "missing_label_gap"
    ]
    full_scope_decision_bucket_counts = {
        bucket: int(bucket_counts.get(bucket, 0)) for bucket in decision_bucket_names
    }
    classification_v2_counts = _auto_triage_policy_classification_counts(bucket_counts)
    full_scope_decision_row_count = sum(full_scope_decision_bucket_counts.values())
    full_scope_manual_review_recommended_count = sum(
        count
        for bucket, count in full_scope_decision_bucket_counts.items()
        if bucket != "auto_pass_candidate"
    )
    sampled_missing_gap_rows = sum(
        1 for row in triage_rows if row.get("recommendation_bucket") == "missing_label_gap"
    )
    full_scope_row_count = len(target_rows) + missing_gap_count
    output_limit_applied = len(emitted_rows) < len(triage_rows) or (
        missing_gap_count > sampled_missing_gap_rows
    )
    decision_summary = {
        "full_scope_row_count": full_scope_row_count,
        "full_scope_candidate_row_count": len(target_rows),
        "full_scope_decision_row_count": full_scope_decision_row_count,
        "full_scope_decision_bucket_counts": full_scope_decision_bucket_counts,
        "full_scope_classification_v2_counts": classification_v2_counts,
        "full_scope_manual_review_recommended_count": full_scope_manual_review_recommended_count,
        "full_scope_auto_pass_candidate_count": int(
            bucket_counts.get("auto_pass_candidate", 0)
        ),
        "full_scope_already_trusted_reviewed_count": int(
            bucket_counts.get("already_trusted_reviewed", 0)
        ),
        "full_scope_missing_label_gap_count": missing_gap_count,
        "full_scope_orphan_raw_concept_backlog_count": orphan_missing_gap_count,
        "emitted_row_count": len(emitted_rows),
        "emitted_decision_row_count": len(decision_rows),
        "emitted_already_trusted_reviewed_count": len(already_trusted_emitted_rows),
        "sampled_missing_label_gap_rows": sampled_missing_gap_rows,
        "output_limit": max_rows,
        "output_limit_applied": output_limit_applied,
        "csv_decision_rows_exclude_buckets": sorted(
            KSA_LABEL_AUTO_TRIAGE_NON_DECISION_BUCKETS
        ),
        "missing_label_gap_is_not_button_row": True,
        "auto_pass_candidate_is_not_human_approval": True,
    }
    target_scope_is_filtered = any(
        value not in (None, "")
        for value in (major_code, middle_code, small_code, sub_code)
    )
    scope_policy = {
        "target_scope_is_filtered": target_scope_is_filtered,
        "scoped_counts_are_local_view": target_scope_is_filtered,
        "scoped_report_is_canonical_bulk_plan": False,
        "scoped_auto_pass_is_not_global_approval": True,
        "all_scope_required_for_bulk_planning": True,
        "canonical_policy_v2_source": (
            "all-scope ksa-label-auto-triage-report with no major/middle/small/sub filters"
        ),
        "operator_sampling_plan_required_before_bulk_use": True,
    }
    operator_strategy = _auto_triage_operator_strategy(
        bucket_counts=bucket_counts,
        major_bucket_rollup=major_bucket_rollup,
        decision_summary=decision_summary,
    )
    return {
        "schema": LABEL_AUTO_TRIAGE_REPORT_SCHEMA,
        "generated_at": _now_iso(),
        "ok": True,
        "status": "review_required",
        "report_only": True,
        "scope": {
            "major_code": major_code,
            "middle_code": middle_code,
            "small_code": small_code,
            "sub_code": sub_code,
            "trusted_major_code": trusted_major_code,
            "trusted_middle_code": trusted_middle_code,
            "trusted_small_code": trusted_small_code,
            "limit": max_rows,
            "sample_limit": max_samples,
        },
        "scope_policy": scope_policy,
        "counts": {
            "concepts_in_scope": concept_scope_count,
            "label_candidates": len(target_rows),
            "missing_label_gap": missing_gap_count,
            "reviewable_missing_label_gap": missing_gap_count,
            "orphan_raw_concept_backlog": orphan_missing_gap_count,
            "trusted_sample_label_candidates": len(trusted_rows),
            "trusted_sample_clean_rows": int(trusted_stats["clean_row_count"]),
            "trusted_sample_candidate_clean_rows": int(
                trusted_stats["candidate_clean_row_count"]
            ),
            "full_scope_rows": full_scope_row_count,
            "full_scope_decision_rows": full_scope_decision_row_count,
            "full_scope_manual_review_recommended_rows": full_scope_manual_review_recommended_count,
            "full_scope_auto_pass_candidate_rows": int(
                bucket_counts.get("auto_pass_candidate", 0)
            ),
            "full_scope_already_trusted_reviewed_rows": int(
                bucket_counts.get("already_trusted_reviewed", 0)
            ),
            "full_scope_missing_label_gaps": missing_gap_count,
            "full_scope_orphan_raw_concept_backlog": orphan_missing_gap_count,
            "emitted_rows": len(emitted_rows),
            "emitted_decision_rows": len(decision_rows),
            "decision_sheet_rows": len(decision_rows),
            "already_trusted_reviewed_rows": len(already_trusted_emitted_rows),
        },
        "status_counts": dict(status_counts),
        "bucket_counts": dict(bucket_counts),
        "classification_bucket_counts": dict(bucket_counts),
        "classification_v2_schema": KSA_LABEL_AUTO_TRIAGE_POLICY_CLASSIFICATION_SCHEMA,
        "classification_v2_map": _auto_triage_policy_classification_map(),
        "classification_v2_counts": classification_v2_counts,
        "policy_classification_counts": classification_v2_counts,
        "major_bucket_rollup": major_bucket_rollup,
        "candidate_count": len(target_rows),
        "trusted_sample_count": len(trusted_rows),
        "emitted_row_count": len(emitted_rows),
        "emitted_decision_row_count": len(decision_rows),
        "decision_row_count": len(decision_rows),
        "already_trusted_reviewed_row_count": len(already_trusted_emitted_rows),
        "full_scope_row_count": full_scope_row_count,
        "full_scope_decision_bucket_counts": full_scope_decision_bucket_counts,
        "full_scope_decision_row_count": full_scope_decision_row_count,
        "full_scope_manual_review_recommended_count": full_scope_manual_review_recommended_count,
        "full_scope_auto_pass_candidate_count": int(
            bucket_counts.get("auto_pass_candidate", 0)
        ),
        "full_scope_already_trusted_reviewed_count": int(
            bucket_counts.get("already_trusted_reviewed", 0)
        ),
        "full_scope_missing_label_gap_count": missing_gap_count,
        "full_scope_orphan_raw_concept_backlog_count": orphan_missing_gap_count,
        "output_limit_applied": output_limit_applied,
        "decision_summary": decision_summary,
        "operator_strategy": operator_strategy,
        "quality_flag_counts": dict(quality_flag_counts),
        "source_method_counts": dict(source_method_counts),
        "top_rules": _top_counter_items(rule_counts, 20),
        "top_quality_flags": _top_counter_items(quality_flag_counts, 20),
        "top_source_scopes": _top_counter_items(target_scope_counts, 20),
        "trusted_sample": {
            "scope": {
                "major_code": trusted_major_code,
                "middle_code": trusted_middle_code,
                "small_code": trusted_small_code,
            },
            "row_count": int(trusted_stats["row_count"]),
            "clean_row_count": int(trusted_stats["clean_row_count"]),
            "status_counts": dict(trusted_stats["status_counts"]),
            "audited_status_counts": dict(trusted_stats["audited_status_counts"]),
            "quality_flag_counts": dict(trusted_stats["quality_flag_counts"]),
            "source_method_counts": dict(trusted_stats["source_methods"]),
            "top_clean_label_keys": _top_counter_items(trusted_stats["clean_label_keys"], 20),
            "top_clean_patterns": _top_counter_items(trusted_stats["clean_patterns"], 20),
            "clean_rows_require_audited_human_review": True,
            "trusted_scope_is_sample_basis_only": True,
            "trusted_scope_is_not_human_approval": True,
            "audit_requirements": [
                "non-automated reviewer_id",
                "notes",
                "source_decision_packet",
                "rationale",
            ],
        },
        "rows": emitted_rows,
        "decision_rows": decision_rows,
        "samples": emitted_rows[:max_samples],
        "samples_by_bucket": samples_by_bucket,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_reviewed_written_by_report": False,
        "safety": {
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "human_reviewed_written_by_report": False,
            "accepted_written_by_report": False,
            "reviewed_written_by_report": False,
            "llm_reviewed_is_human_approval": False,
            "trusted_sample_scope_is_not_approval": True,
            "trusted_sample_requires_audited_human_review": True,
            "auto_pass_candidate_is_not_human_approval": True,
            "already_trusted_reviewed_bucket_is_not_review_queue": True,
            "orphan_raw_concepts_excluded_from_missing_label_gap": True,
            "scoped_counts_are_local_view": target_scope_is_filtered,
            "all_scope_required_for_bulk_planning": True,
        },
        "operator_guidance": [
            "This report classifies recommendation buckets only; it does not write statuses.",
            "Recommendation buckets are not human approval and must not be treated as trusted review status.",
            "auto_pass_candidate means low-risk machine recommendation candidate, not human approval.",
            "Scoped reports are local views; use the all-scope policy-v2 report and sampling plan for bulk planning.",
            "already_trusted_reviewed rows are separated from the active triage queue because they already carry audited human-review provenance.",
            "trusted_* scope rows are sample evidence only and do not grant human-reviewed status.",
            "trusted sample support is counted only from audited human review rows with source_decision_packet and rationale.",
            "missing_label_gap rows identify linked concepts that need candidate generation or review context.",
            "orphan raw concepts without source evidence are counted separately and excluded from review rows.",
        ],
    }


def write_ksa_label_auto_triage_report_markdown(
    report: dict[str, Any],
    out_path: Path,
) -> None:
    lines = [
        "# KSA Short Label Auto Triage Report",
        "",
        "This read-only report classifies KSA short-label recommendation candidates using an HR sample scope.",
        "",
        "## Summary",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- schema: `{report.get('schema')}`",
        f"- status: `{report.get('status')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        f"- human_reviewed_written_by_report: `{report.get('human_reviewed_written_by_report')}`",
        "",
        "## Counts",
        "",
        "| metric | count |",
        "|---|---:|",
    ]
    for key, count in sorted((report.get("counts") or {}).items()):
        lines.append(f"| `{_md_cell(key)}` | {count} |")
    scope_policy = report.get("scope_policy") or {}
    lines.extend(
        [
            "",
            "## Scope Policy",
            "",
            f"- target_scope_is_filtered: `{scope_policy.get('target_scope_is_filtered')}`",
            f"- scoped_counts_are_local_view: `{scope_policy.get('scoped_counts_are_local_view')}`",
            f"- scoped_report_is_canonical_bulk_plan: `{scope_policy.get('scoped_report_is_canonical_bulk_plan')}`",
            f"- all_scope_required_for_bulk_planning: `{scope_policy.get('all_scope_required_for_bulk_planning')}`",
            f"- canonical_policy_v2_source: `{_md_cell(scope_policy.get('canonical_policy_v2_source'))}`",
            "- scoped auto-pass counts are not global approval evidence.",
        ]
    )
    decision_summary = report.get("decision_summary") or {}
    lines.extend(
        [
            "",
            "## Operator Workload",
            "",
            "| metric | count |",
            "|---|---:|",
            (
                "| `full_scope_decision_row_count` | "
                f"{decision_summary.get('full_scope_decision_row_count', report.get('full_scope_decision_row_count', 0))} |"
            ),
            (
                "| `full_scope_manual_review_recommended_count` | "
                f"{decision_summary.get('full_scope_manual_review_recommended_count', report.get('full_scope_manual_review_recommended_count', 0))} |"
            ),
            (
                "| `full_scope_auto_pass_candidate_count` | "
                f"{decision_summary.get('full_scope_auto_pass_candidate_count', report.get('full_scope_auto_pass_candidate_count', 0))} |"
            ),
            (
                "| `full_scope_already_trusted_reviewed_count` | "
                f"{decision_summary.get('full_scope_already_trusted_reviewed_count', report.get('full_scope_already_trusted_reviewed_count', 0))} |"
            ),
            (
                "| `full_scope_missing_label_gap_count` | "
                f"{decision_summary.get('full_scope_missing_label_gap_count', report.get('full_scope_missing_label_gap_count', 0))} |"
            ),
            (
                "| `full_scope_orphan_raw_concept_backlog_count` | "
                f"{decision_summary.get('full_scope_orphan_raw_concept_backlog_count', report.get('full_scope_orphan_raw_concept_backlog_count', 0))} |"
            ),
            (
                "| `emitted_decision_row_count` | "
                f"{decision_summary.get('emitted_decision_row_count', report.get('emitted_decision_row_count', report.get('decision_row_count', 0)))} |"
            ),
        ]
    )
    lines.extend(
        [
            "",
            "`full_scope_decision_row_count` is the active label-candidate workload excluding already trusted rows and missing-label gaps.",
            "`emitted_decision_row_count` is the capped CSV/display workload for this report run.",
            "`missing_label_gap` is counted separately because it has source evidence but no label-candidate button row yet.",
            "`orphan_raw_concept_backlog` is not emitted as a human review row because it has no source evidence to inspect.",
        ]
    )
    lines.extend(
        [
            "",
            "## Bucket Counts",
            "",
            "| bucket | count |",
            "|---|---:|",
        ]
    )
    for bucket in KSA_LABEL_AUTO_TRIAGE_BUCKETS:
        count = (report.get("bucket_counts") or {}).get(bucket, 0)
        lines.append(f"| `{_md_cell(bucket)}` | {count} |")
    lines.extend(
        [
            "",
            "`auto_pass_candidate` is a machine triage recommendation, not human approval.",
            "`already_trusted_reviewed` is separated from the active candidate queue.",
        ]
    )
    classification_v2_counts = report.get("classification_v2_counts") or {}
    if classification_v2_counts:
        lines.extend(
            [
                "",
                "## Policy Classifications",
                "",
                f"- schema: `{_md_cell(report.get('classification_v2_schema'))}`",
                "",
                "| classification_v2 | count | reason |",
                "|---|---:|---|",
            ]
        )
        classification_map = report.get("classification_v2_map") or {}
        for classification in KSA_LABEL_AUTO_TRIAGE_POLICY_CLASSIFICATIONS:
            reason = ""
            for mapped in classification_map.values():
                if mapped.get("classification_v2") == classification:
                    reason = str(mapped.get("classification_reason") or "")
                    break
            lines.append(
                "| "
                f"`{_md_cell(classification)}` | "
                f"{int(classification_v2_counts.get(classification) or 0)} | "
                f"{_md_cell(reason)} |"
            )
    strategy = report.get("operator_strategy") or {}
    if strategy:
        lines.extend(
            [
                "",
                "## Review Strategy",
                "",
                f"- strategy: `{_md_cell(strategy.get('strategy'))}`",
                f"- bulk_human_review_recommended: `{strategy.get('bulk_human_review_recommended')}`",
                f"- manual_review_recommended_count: `{strategy.get('manual_review_recommended_count')}`",
                f"- auto_pass_candidate_count: `{strategy.get('auto_pass_candidate_count')}`",
                f"- auto_pass_is_not_human_approval: `{strategy.get('auto_pass_is_not_human_approval')}`",
                "",
                "| order | step | bucket | rows | reason |",
                "|---:|---|---|---:|---|",
            ]
        )
        for step in strategy.get("recommended_sequence") or []:
            lines.append(
                "| "
                f"{int(step.get('order') or 0)} | "
                f"`{_md_cell(step.get('step'))}` | "
                f"`{_md_cell(step.get('bucket'))}` | "
                f"{int(step.get('row_count') or 0)} | "
                f"{_md_cell(step.get('reason'))} |"
            )
        top_manual_majors = strategy.get("top_manual_review_majors") or []
        if top_manual_majors:
            lines.extend(
                [
                    "",
                    "### Top Manual Review Majors",
                    "",
                    "| major | name | manual recommended | decision rows | domain expert | revise |",
                    "|---|---|---:|---:|---:|---:|",
                ]
            )
            for item in top_manual_majors:
                lines.append(
                    "| "
                    f"`{_md_cell(item.get('major_code'))}` | "
                    f"{_md_cell(item.get('major_name'))} | "
                    f"{int(item.get('manual_review_recommended_count') or 0)} | "
                    f"{int(item.get('decision_row_count') or 0)} | "
                    f"{int(item.get('domain_expert_required_count') or 0)} | "
                    f"{int(item.get('revise_recommended_count') or 0)} |"
                )
        for note in strategy.get("operator_notes") or []:
            lines.append(f"- {_md_cell(note)}")
    major_rollup = report.get("major_bucket_rollup") or []
    if major_rollup:
        lines.extend(
            [
                "",
                "## Major Bucket Rollup",
                "",
                "| major | name | rows | decision rows | manual recommended | auto-pass candidates | domain expert | human sample | revise | already trusted |",
                "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for item in major_rollup:
            if not isinstance(item, dict):
                continue
            bucket_counts = (
                item.get("bucket_counts")
                if isinstance(item.get("bucket_counts"), dict)
                else {}
            )
            lines.append(
                "| "
                f"`{_md_cell(item.get('major_code'))}` | "
                f"{_md_cell(item.get('major_name'))} | "
                f"{int(item.get('row_count') or 0)} | "
                f"{int(item.get('decision_row_count') or 0)} | "
                f"{int(item.get('manual_review_recommended_count') or 0)} | "
                f"{int(item.get('auto_pass_candidate_count') or 0)} | "
                f"{int(bucket_counts.get('domain_expert_required') or 0)} | "
                f"{int(bucket_counts.get('human_sample_required') or 0)} | "
                f"{int(bucket_counts.get('revise_recommended') or 0)} | "
                f"{int(item.get('already_trusted_reviewed_count') or 0)} |"
            )
    lines.extend(
        [
            "",
            "## Status Counts",
            "",
            "| status | count |",
            "|---|---:|",
        ]
    )
    status_counts = report.get("status_counts") or {}
    if status_counts:
        for status, count in sorted(status_counts.items()):
            lines.append(f"| `{_md_cell(status)}` | {count} |")
    else:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## Top Rules",
            "",
            "| rule | count |",
            "|---|---:|",
        ]
    )
    top_rules = report.get("top_rules") or []
    if top_rules:
        for item in top_rules:
            lines.append(f"| `{_md_cell(item.get('key'))}` | {item.get('count')} |")
    else:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## Samples By Bucket",
            "",
            "| bucket | classification_v2 | rule | concept | label | source | flags | HR sample support | approval state |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    )
    any_sample = False
    for bucket in KSA_LABEL_AUTO_TRIAGE_BUCKETS:
        for sample in (report.get("samples_by_bucket") or {}).get(bucket) or []:
            any_sample = True
            flags = "; ".join(sample.get("quality_flags") or [])
            concept = f"#{sample.get('concept_id')} {sample.get('concept_type') or ''} {sample.get('concept_name') or ''}"
            support = (
                f"{sample.get('hr_sample_support')}; "
                f"key={sample.get('hr_sample_label_key_count')}; "
                f"pattern={sample.get('hr_sample_pattern_count')}"
            )
            approval_state = (
                f"audited={sample.get('audited_trusted_review')}; "
                f"missing={sample.get('human_approval_missing')}; "
                f"not_approval={sample.get('candidate_bucket_is_not_approval')}"
            )
            lines.append(
                "| "
                f"`{_md_cell(bucket)}` | "
                f"`{_md_cell(sample.get('classification_v2'))}` | "
                f"`{_md_cell(sample.get('recommendation_rule'))}` | "
                f"{_md_cell(concept)} | "
                f"{_md_cell(sample.get('label_text'))} | "
                f"{_md_cell(sample.get('source_text'))} | "
                f"{_md_cell(flags)} | "
                f"{_md_cell(support)} | "
                f"{_md_cell(approval_state)} |"
            )
    if not any_sample:
        lines.append("| none |  |  |  |  |  |  |  |  |")
    lines.extend(["", "## Operator Guidance", ""])
    for item in report.get("operator_guidance") or []:
        lines.append(f"- {_md_cell(item)}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_ksa_label_auto_triage_report_csv(
    report: dict[str, Any],
    out_path: Path,
) -> dict[str, Any]:
    fieldnames = [
        "schema",
        "recommendation_bucket",
        "classification_v2",
        "classification_v2_schema",
        "classification_reason",
        "requires_human_sample",
        "requires_domain_expert",
        "classification_v2_is_decision_row",
        "recommendation_rule",
        "recommendation_rationale",
        "label_id",
        "concept_id",
        "concept_type",
        "concept_name",
        "source_ksa_id",
        "source_atomic_id",
        "source_scope_key",
        "source_text",
        "raw_ksa_text",
        "atomic_ksa_text",
        "label_text",
        "normalized_label_key",
        "source_method",
        "method_details",
        "confidence_score",
        "review_status",
        "audited_trusted_review",
        "human_approval_missing",
        "recommendation_is_human_approval",
        "candidate_bucket_is_not_approval",
        "operator_trust_state",
        "quality_flags",
        "short_label_transform_state",
        "short_label_source_length",
        "short_label_label_length",
        "short_label_removed_char_count",
        "short_label_length_ratio",
        "hr_sample_support",
        "hr_sample_label_key_count",
        "hr_sample_pattern_count",
        "hr_sample_pattern_key",
        "collision_row_count",
        "collision_concept_count",
        "collision_scope_count",
        "collision_major_count",
        "status_update_allowed",
        "db_writes",
        "approval_claim",
        "human_reviewed_written_by_report",
        "decision",
        "reviewer_id",
        "reviewed_at",
        "rationale",
    ]
    if "decision_rows" in report:
        rows = [
            row
            for row in report.get("decision_rows") or []
            if row.get("recommendation_bucket") not in KSA_LABEL_AUTO_TRIAGE_NON_DECISION_BUCKETS
        ]
    else:
        rows = [
            row
            for row in report.get("rows") or []
            if row.get("recommendation_bucket") not in KSA_LABEL_AUTO_TRIAGE_NON_DECISION_BUCKETS
        ]
    source_rows = report.get("rows") or []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            csv_row: dict[str, Any] = {"schema": LABEL_AUTO_TRIAGE_CSV_SCHEMA}
            for field in fieldnames:
                if field == "schema":
                    continue
                value = "" if field in {"decision", "reviewer_id", "reviewed_at", "rationale"} else row.get(field, "")
                if isinstance(value, list):
                    value = ";".join(str(item) for item in value)
                csv_row[field] = _csv_safe_cell(value)
            writer.writerow(csv_row)
    return {
        "path": str(out_path),
        "record_count": len(rows),
        "source_row_count": len(source_rows),
        "excluded_already_trusted_reviewed_count": sum(
            1
            for row in source_rows
            if row.get("recommendation_bucket") == "already_trusted_reviewed"
        ),
        "schema": LABEL_AUTO_TRIAGE_CSV_SCHEMA,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_reviewed_written_by_report": False,
    }


KSA_LABEL_POLICY_V2_SAMPLE_RULES = (
    {
        "classification_v2": "modify-recommended",
        "source_bucket": "revise_recommended",
        "recommended_phase": "rule_revision_first",
        "sample_rate": 0.01,
        "sample_floor": 30,
        "sample_cap": 200,
        "operator_note": "Fix label transformation rules before row review.",
    },
    {
        "classification_v2": "domain-expert-needed",
        "source_bucket": "domain_expert_required",
        "recommended_phase": "domain_expert_sample",
        "sample_rate": 0.02,
        "sample_floor": 20,
        "sample_cap": 150,
        "operator_note": "Route technical, acronym, and symbol-heavy samples to domain specialists.",
    },
    {
        "classification_v2": "human-sample-needed",
        "source_bucket": "human_sample_required",
        "recommended_phase": "major_pattern_sample",
        "sample_rate": 0.005,
        "sample_floor": 30,
        "sample_cap": 150,
        "operator_note": "Stratify by major, pattern, and label family; do not bulk approve.",
    },
    {
        "classification_v2": "auto-pass-candidate",
        "source_bucket": "auto_pass_candidate",
        "recommended_phase": "spotcheck_only",
        "sample_rate": 1.0,
        "sample_floor": 0,
        "sample_cap": 10,
        "operator_note": "Spot-check only; this is not approval.",
    },
)


def _policy_v2_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _policy_v2_sample_size(
    count: Any,
    *,
    sample_rate: Any,
    sample_floor: Any,
    sample_cap: Any,
) -> int:
    row_count = _policy_v2_int(count)
    if row_count <= 0:
        return 0
    rate = float(sample_rate or 0)
    floor = _policy_v2_int(sample_floor)
    cap = _policy_v2_int(sample_cap)
    if rate >= 1.0:
        return min(row_count, cap)
    return min(max(floor, math.ceil(row_count * rate)), cap, row_count)


def _ksa_label_policy_v2_sampling_plan_source_issues(
    source_report: dict[str, Any],
    *,
    require_all_scope: bool = True,
) -> list[str]:
    issues: list[str] = []
    if source_report.get("schema") != LABEL_AUTO_TRIAGE_REPORT_SCHEMA:
        issues.append("source_schema_not_ksa_label_auto_triage_report_v1")
    if source_report.get("ok") is not True:
        issues.append("source_ok_not_true")
    if source_report.get("report_only") is not True:
        issues.append("source_report_only_not_true")
    for field in ("status_update_allowed", "db_writes", "approval_claim"):
        if source_report.get(field) is not False:
            issues.append(f"{field}_not_false")
    safety = source_report.get("safety") if isinstance(source_report.get("safety"), dict) else {}
    for field in (
        "status_update_allowed",
        "db_writes",
        "approval_claim",
        "human_reviewed_written_by_report",
        "accepted_written_by_report",
        "reviewed_written_by_report",
    ):
        if safety.get(field) is not False:
            issues.append(f"safety.{field}_not_false")
    scope_policy = (
        source_report.get("scope_policy")
        if isinstance(source_report.get("scope_policy"), dict)
        else {}
    )
    if not scope_policy:
        issues.append("scope_policy_missing")
    elif require_all_scope and scope_policy.get("target_scope_is_filtered") is not False:
        issues.append("source_report_scope_filtered")
    if not isinstance(source_report.get("classification_v2_counts"), dict):
        issues.append("classification_v2_counts_missing")
    if not isinstance(source_report.get("major_bucket_rollup"), list):
        issues.append("major_bucket_rollup_missing")
    classification_v2_counts = (
        source_report.get("classification_v2_counts")
        if isinstance(source_report.get("classification_v2_counts"), dict)
        else {}
    )
    major_rollup = (
        source_report.get("major_bucket_rollup")
        if isinstance(source_report.get("major_bucket_rollup"), list)
        else []
    )
    if major_rollup:
        full_scope_decision_rows = _policy_v2_int(
            source_report.get("full_scope_decision_row_count")
            or (source_report.get("decision_summary") or {}).get("full_scope_decision_row_count")
        )
        major_decision_rows = sum(
            _policy_v2_int(item.get("decision_row_count"))
            for item in major_rollup
            if isinstance(item, dict)
        )
        if full_scope_decision_rows and major_decision_rows != full_scope_decision_rows:
            issues.append(
                "major_decision_row_count_mismatch:"
                f"{major_decision_rows}!={full_scope_decision_rows}"
            )
        aggregate_bucket_counts: dict[str, int] = {}
        for item in major_rollup:
            if not isinstance(item, dict) or not isinstance(item.get("bucket_counts"), dict):
                continue
            for bucket, count in item["bucket_counts"].items():
                aggregate_bucket_counts[str(bucket)] = aggregate_bucket_counts.get(str(bucket), 0) + _policy_v2_int(count)
        aggregate_classification_counts = _auto_triage_policy_classification_counts(
            aggregate_bucket_counts
        )
        for classification in KSA_LABEL_AUTO_TRIAGE_POLICY_CLASSIFICATIONS:
            if classification not in classification_v2_counts:
                issues.append(f"classification_v2_count_missing:{classification}")
                continue
            expected = _policy_v2_int(classification_v2_counts.get(classification))
            actual = _policy_v2_int(aggregate_classification_counts.get(classification))
            if actual != expected:
                issues.append(
                    "classification_v2_count_mismatch:"
                    f"{classification}:{actual}!={expected}"
                )
    return issues


def build_ksa_label_policy_v2_sampling_plan(
    source_report: dict[str, Any],
    *,
    source_report_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build a report-only operator sampling plan from a policy-v2 triage report."""
    source_issues = _ksa_label_policy_v2_sampling_plan_source_issues(source_report)
    if source_issues:
        return {
            "schema": LABEL_POLICY_V2_SAMPLING_PLAN_SCHEMA,
            "generated_at": _now_iso(),
            "ok": False,
            "status": "invalid_source_report",
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "human_reviewed_written_by_report": False,
            "accepted_written_by_report": False,
            "reviewed_written_by_report": False,
            "source_report": str(source_report_path or ""),
            "source_schema": source_report.get("schema"),
            "source_issues": source_issues,
            "summary": {},
            "major_summaries": [],
            "sample_plan_rows": [],
            "forbidden_use": [
                "Do not set human_reviewed, accepted, or reviewed from this plan.",
                "Do not rewrite ksa_items.ksa_text_raw.",
                "Do not treat sample completion as full approval.",
            ],
        }

    major_summaries: list[dict[str, Any]] = []
    sample_plan_rows: list[dict[str, Any]] = []
    for major in sorted(
        source_report.get("major_bucket_rollup") or [],
        key=lambda item: str(item.get("major_code") or ""),
    ):
        if not isinstance(major, dict):
            continue
        bucket_counts = (
            major.get("bucket_counts") if isinstance(major.get("bucket_counts"), dict) else {}
        )
        decision_rows = _policy_v2_int(major.get("decision_row_count"))
        recommended_sample_rows = 0
        for rule in KSA_LABEL_POLICY_V2_SAMPLE_RULES:
            bucket_count = _policy_v2_int(bucket_counts.get(rule["source_bucket"]))
            sample_rows = _policy_v2_sample_size(
                bucket_count,
                sample_rate=rule["sample_rate"],
                sample_floor=rule["sample_floor"],
                sample_cap=rule["sample_cap"],
            )
            recommended_sample_rows += sample_rows
            sample_plan_rows.append(
                {
                    "schema": LABEL_POLICY_V2_SAMPLING_PLAN_CSV_SCHEMA,
                    "major_code": major.get("major_code"),
                    "major_name": major.get("major_name"),
                    "classification_v2": rule["classification_v2"],
                    "source_bucket": rule["source_bucket"],
                    "recommended_phase": rule["recommended_phase"],
                    "decision_rows": bucket_count,
                    "sample_rate": rule["sample_rate"],
                    "sample_floor": rule["sample_floor"],
                    "sample_cap": rule["sample_cap"],
                    "recommended_sample_rows": sample_rows,
                    "sample_is_approval": False,
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                    "human_reviewed_written_by_report": False,
                    "operator_note": rule["operator_note"],
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "rationale": "",
                }
            )
        major_summaries.append(
            {
                "major_code": major.get("major_code"),
                "major_name": major.get("major_name"),
                "decision_rows": decision_rows,
                "manual_review_recommended_count": _policy_v2_int(
                    major.get("manual_review_recommended_count")
                ),
                "recommended_sample_rows": recommended_sample_rows,
                "estimated_click_reduction_ratio": round(
                    1 - (recommended_sample_rows / decision_rows),
                    6,
                )
                if decision_rows
                else None,
                "bucket_counts": dict(bucket_counts),
            }
        )

    decision_rows_total = sum(item["decision_rows"] for item in major_summaries)
    sample_rows_total = sum(item["recommended_sample_rows"] for item in major_summaries)
    summary = {
        "source_report": str(source_report_path or ""),
        "source_schema": source_report.get("schema"),
        "candidate_count": _policy_v2_int(source_report.get("candidate_count")),
        "full_scope_decision_row_count": _policy_v2_int(
            source_report.get("full_scope_decision_row_count")
            or (source_report.get("decision_summary") or {}).get("full_scope_decision_row_count")
        ),
        "classification_v2_counts": source_report.get("classification_v2_counts") or {},
        "major_count": len(major_summaries),
        "recommended_sample_rows_total": sample_rows_total,
        "decision_rows_total_from_major_rollup": decision_rows_total,
        "estimated_click_reduction_ratio": round(
            1 - (sample_rows_total / decision_rows_total),
            6,
        )
        if decision_rows_total
        else None,
    }
    return {
        "schema": LABEL_POLICY_V2_SAMPLING_PLAN_SCHEMA,
        "generated_at": _now_iso(),
        "ok": True,
        "status": "review_planning_only",
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_reviewed_written_by_report": False,
        "accepted_written_by_report": False,
        "reviewed_written_by_report": False,
        "source_report": str(source_report_path or ""),
        "source_schema": source_report.get("schema"),
        "source_issues": [],
        "summary": summary,
        "sampling_policy": {
            "purpose": "reduce operator workload by stratified sampling; not approval automation",
            "sequence": [
                "modify-recommended: inspect samples and revise transformation rules first",
                "domain-expert-needed: send technical, acronym, and symbol-heavy samples to domain specialists",
                "human-sample-needed: sample by major, pattern, and label family before broader operation",
                "auto-pass-candidate: spot-check only; never convert to human approval automatically",
            ],
            "rules": list(KSA_LABEL_POLICY_V2_SAMPLE_RULES),
        },
        "major_summaries": major_summaries,
        "sample_plan_rows": sample_plan_rows,
        "forbidden_use": [
            "Do not set human_reviewed, accepted, or reviewed from this plan.",
            "Do not rewrite ksa_items.ksa_text_raw.",
            "Do not treat sample completion as full approval.",
            "Do not use auto-pass-candidate as a trusted status without human spot-check evidence.",
        ],
    }


def write_ksa_label_policy_v2_sampling_plan_markdown(
    plan: dict[str, Any],
    out_path: Path,
) -> None:
    summary = plan.get("summary") if isinstance(plan.get("summary"), dict) else {}
    lines = [
        "# KSA Label Policy V2 Operator Sampling Plan",
        "",
        f"- ok: `{plan.get('ok')}`",
        f"- status: `{plan.get('status')}`",
        f"- source_report: `{_md_cell(plan.get('source_report'))}`",
        f"- status_update_allowed: `{plan.get('status_update_allowed')}`",
        f"- db_writes: `{plan.get('db_writes')}`",
        f"- approval_claim: `{plan.get('approval_claim')}`",
        f"- full_scope_decision_row_count: `{summary.get('full_scope_decision_row_count')}`",
        f"- recommended_sample_rows_total: `{summary.get('recommended_sample_rows_total')}`",
        f"- estimated_click_reduction_ratio: `{summary.get('estimated_click_reduction_ratio')}`",
        "",
    ]
    if plan.get("source_issues"):
        lines.extend(["## Source Issues", ""])
        for issue in plan.get("source_issues") or []:
            lines.append(f"- `{_md_cell(issue)}`")
    sampling_policy = plan.get("sampling_policy") if isinstance(plan.get("sampling_policy"), dict) else {}
    if sampling_policy:
        lines.extend(["## Sampling Policy", ""])
        for step in sampling_policy.get("sequence") or []:
            lines.append(f"- {_md_cell(step)}")
        lines.append("")
    major_summaries = plan.get("major_summaries") or []
    lines.extend(
        [
            "## Major Summary",
            "",
            "| major | name | decision rows | recommended sample | estimated click reduction | modify | domain expert | human sample | auto spotcheck |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    if major_summaries:
        for item in sorted(
            major_summaries,
            key=lambda row: _policy_v2_int(row.get("decision_rows")),
            reverse=True,
        ):
            buckets = item.get("bucket_counts") if isinstance(item.get("bucket_counts"), dict) else {}
            lines.append(
                "| "
                f"`{_md_cell(item.get('major_code'))}` | "
                f"{_md_cell(item.get('major_name'))} | "
                f"{_policy_v2_int(item.get('decision_rows'))} | "
                f"{_policy_v2_int(item.get('recommended_sample_rows'))} | "
                f"{item.get('estimated_click_reduction_ratio')} | "
                f"{_policy_v2_int(buckets.get('revise_recommended'))} | "
                f"{_policy_v2_int(buckets.get('domain_expert_required'))} | "
                f"{_policy_v2_int(buckets.get('human_sample_required'))} | "
                f"{_policy_v2_int(buckets.get('auto_pass_candidate'))} |"
            )
    else:
        lines.append("| none |  | 0 | 0 |  | 0 | 0 | 0 | 0 |")
    lines.extend(["", "## Forbidden Use", ""])
    for rule in plan.get("forbidden_use") or []:
        lines.append(f"- {_md_cell(rule)}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_ksa_label_policy_v2_sampling_plan_csv(
    plan: dict[str, Any],
    out_path: Path,
) -> dict[str, Any]:
    fieldnames = [
        "schema",
        "major_code",
        "major_name",
        "classification_v2",
        "source_bucket",
        "recommended_phase",
        "decision_rows",
        "sample_rate",
        "sample_floor",
        "sample_cap",
        "recommended_sample_rows",
        "sample_is_approval",
        "status_update_allowed",
        "db_writes",
        "approval_claim",
        "human_reviewed_written_by_report",
        "operator_note",
        "decision",
        "reviewer_id",
        "reviewed_at",
        "rationale",
    ]
    rows = plan.get("sample_plan_rows") or []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: _csv_safe_cell(
                        "" if field in {"decision", "reviewer_id", "reviewed_at", "rationale"} else row.get(field, "")
                    )
                    for field in fieldnames
                }
            )
    return {
        "path": str(out_path),
        "schema": LABEL_POLICY_V2_SAMPLING_PLAN_CSV_SCHEMA,
        "record_count": len(rows),
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_reviewed_written_by_report": False,
    }


def _policy_v2_major_rollup_for_code(
    report: dict[str, Any],
    major_code: str,
) -> dict[str, Any] | None:
    normalized = str(major_code or "").zfill(2)
    for item in report.get("major_bucket_rollup") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("major_code") or "").zfill(2) == normalized:
            return item
    return None


def _policy_v2_classification_counts_from_major_rollup(
    major_rollup: dict[str, Any] | None,
) -> dict[str, int]:
    bucket_counts = (
        major_rollup.get("bucket_counts")
        if isinstance(major_rollup, dict) and isinstance(major_rollup.get("bucket_counts"), dict)
        else {}
    )
    return _auto_triage_policy_classification_counts(
        {str(key): _policy_v2_int(value) for key, value in bucket_counts.items()}
    )


def build_ksa_label_policy_v2_scope_diff(
    all_scope_report: dict[str, Any],
    scoped_report: dict[str, Any],
    *,
    major_code: str,
    all_scope_report_path: str | Path | None = None,
    scoped_report_path: str | Path | None = None,
) -> dict[str, Any]:
    """Compare all-scope and major-scoped policy-v2 counts for operator diagnostics."""
    source_issues = [
        f"all_scope:{issue}"
        for issue in _ksa_label_policy_v2_sampling_plan_source_issues(
            all_scope_report,
            require_all_scope=True,
        )
    ]
    source_issues.extend(
        f"scoped:{issue}"
        for issue in _ksa_label_policy_v2_sampling_plan_source_issues(
            scoped_report,
            require_all_scope=False,
        )
    )
    normalized_major = str(major_code or "").zfill(2)
    all_scope_major = _policy_v2_major_rollup_for_code(all_scope_report, normalized_major)
    if all_scope_major is None:
        source_issues.append(f"all_scope_major_rollup_missing:{normalized_major}")
    scoped_scope = scoped_report.get("scope") if isinstance(scoped_report.get("scope"), dict) else {}
    scoped_major = scoped_scope.get("major_code")
    if scoped_major and str(scoped_major).zfill(2) != normalized_major:
        source_issues.append(
            f"scoped_report_major_mismatch:{str(scoped_major).zfill(2)}"
        )

    all_scope_counts = _policy_v2_classification_counts_from_major_rollup(all_scope_major)
    scoped_counts = {
        classification: _policy_v2_int(
            (scoped_report.get("classification_v2_counts") or {}).get(classification)
        )
        for classification in KSA_LABEL_AUTO_TRIAGE_POLICY_CLASSIFICATIONS
    }
    comparisons = []
    for classification in KSA_LABEL_AUTO_TRIAGE_POLICY_CLASSIFICATIONS:
        all_count = _policy_v2_int(all_scope_counts.get(classification))
        scoped_count = _policy_v2_int(scoped_counts.get(classification))
        comparisons.append(
            {
                "classification_v2": classification,
                "all_scope_major_count": all_count,
                "scoped_report_count": scoped_count,
                "difference": scoped_count - all_count,
                "scope_sensitive_difference": scoped_count != all_count,
            }
        )
    differing = [item for item in comparisons if item["scope_sensitive_difference"]]
    all_decision_rows = _policy_v2_int(
        all_scope_major.get("decision_row_count") if isinstance(all_scope_major, dict) else 0
    )
    scoped_decision_rows = _policy_v2_int(
        scoped_report.get("full_scope_decision_row_count")
        or (scoped_report.get("decision_summary") or {}).get("full_scope_decision_row_count")
    )
    return {
        "schema": LABEL_POLICY_V2_SCOPE_DIFF_SCHEMA,
        "generated_at": _now_iso(),
        "ok": not source_issues,
        "status": "diagnostic_only" if not source_issues else "invalid_source_report",
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_reviewed_written_by_report": False,
        "accepted_written_by_report": False,
        "reviewed_written_by_report": False,
        "source_paths": {
            "all_scope_report": str(all_scope_report_path or ""),
            "scoped_report": str(scoped_report_path or ""),
        },
        "source_issues": source_issues,
        "major_code": normalized_major,
        "major_name": all_scope_major.get("major_name")
        if isinstance(all_scope_major, dict)
        else None,
        "summary": {
            "all_scope_major_decision_rows": all_decision_rows,
            "scoped_report_decision_rows": scoped_decision_rows,
            "decision_row_difference": scoped_decision_rows - all_decision_rows,
            "differing_classification_count": len(differing),
            "scope_sensitive_drift_present": bool(differing),
            "all_scope_output_limit_applied": bool(all_scope_report.get("output_limit_applied")),
            "scoped_output_limit_applied": bool(scoped_report.get("output_limit_applied")),
        },
        "all_scope_major_rollup": all_scope_major,
        "scoped_scope": scoped_scope,
        "classification_comparisons": comparisons,
        "operator_guidance": [
            "This is a count-only diagnostic, not approval evidence.",
            "Use all-scope counts for release or cross-major workload planning.",
            "Use scoped runs as local diagnostics; auto-pass counts may drift when collision context changes.",
            "Do not set human_reviewed, accepted, or reviewed from this report.",
        ],
        "forbidden_use": [
            "Do not treat scoped auto-pass count as trusted approval.",
            "Do not write review statuses from this diagnostic.",
            "Do not mutate ksa_items.ksa_text_raw.",
        ],
    }


def write_ksa_label_policy_v2_scope_diff_markdown(
    report: dict[str, Any],
    out_path: Path,
) -> None:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# KSA Label Policy V2 Scope Diff",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- status: `{report.get('status')}`",
        f"- major_code: `{_md_cell(report.get('major_code'))}`",
        f"- major_name: `{_md_cell(report.get('major_name'))}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        f"- scope_sensitive_drift_present: `{summary.get('scope_sensitive_drift_present')}`",
        f"- differing_classification_count: `{summary.get('differing_classification_count')}`",
        "",
    ]
    if report.get("source_issues"):
        lines.extend(["## Source Issues", ""])
        for issue in report.get("source_issues") or []:
            lines.append(f"- `{_md_cell(issue)}`")
        lines.append("")
    lines.extend(
        [
            "## Classification Comparison",
            "",
            "| classification_v2 | all-scope major count | scoped report count | difference | drift |",
            "|---|---:|---:|---:|---|",
        ]
    )
    for item in report.get("classification_comparisons") or []:
        lines.append(
            "| "
            f"`{_md_cell(item.get('classification_v2'))}` | "
            f"{_policy_v2_int(item.get('all_scope_major_count'))} | "
            f"{_policy_v2_int(item.get('scoped_report_count'))} | "
            f"{_policy_v2_int(item.get('difference'))} | "
            f"`{item.get('scope_sensitive_difference')}` |"
        )
    lines.extend(["", "## Operator Guidance", ""])
    for note in report.get("operator_guidance") or []:
        lines.append(f"- {_md_cell(note)}")
    lines.extend(["", "## Forbidden Use", ""])
    for rule in report.get("forbidden_use") or []:
        lines.append(f"- {_md_cell(rule)}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_ksa_missing_label_gap_review_pack_markdown(
    report: dict[str, Any],
    out_path: Path,
) -> None:
    lines = [
        "# KSA Missing Label Gap Review Pack",
        "",
        "This read-only pack lists linked ontology concepts that do not yet have a short-label candidate.",
        "",
        "## Summary",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- schema: `{report.get('schema')}`",
        f"- status: `{report.get('status')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        "",
        "## Counts",
        "",
        "| metric | count |",
        "|---|---:|",
    ]
    for key, count in sorted((report.get("counts") or {}).items()):
        lines.append(f"| `{_md_cell(key)}` | {count} |")
    lines.extend(
        [
            "",
            "`missing_label_gap` rows are not button-style approval rows. They need label candidate generation or review context first.",
            "`orphan_raw_concept_backlog` is counted separately and excluded from CSV rows because there is no source evidence to inspect.",
            "",
            "## Concept Type Counts",
            "",
            "| concept_type | count |",
            "|---|---:|",
        ]
    )
    concept_counts = report.get("concept_type_counts") or {}
    if concept_counts:
        for key, count in sorted(concept_counts.items()):
            lines.append(f"| `{_md_cell(key)}` | {count} |")
    else:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## Samples",
            "",
            "| concept | type | scope | unit | evidence | action |",
            "|---|---|---|---|---|---|",
        ]
    )
    samples = report.get("samples") or []
    if samples:
        for row in samples:
            evidence = row.get("sample_atomic_text") or row.get("sample_ksa_text") or row.get("sample_criteria_text") or ""
            lines.append(
                "| "
                f"#{row.get('concept_id')} {_md_cell(row.get('concept_name'))} | "
                f"`{_md_cell(row.get('concept_type'))}` | "
                f"`{_md_cell(row.get('source_scope_key'))}` | "
                f"{_md_cell(row.get('sample_unit_name'))} | "
                f"{_md_cell(evidence)} | "
                f"`{_md_cell(row.get('operator_action'))}` |"
            )
    else:
        lines.append("| none |  |  |  |  |")
    lines.extend(["", "## Operator Guidance", ""])
    for item in report.get("operator_guidance") or []:
        lines.append(f"- {_md_cell(item)}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_ksa_missing_label_gap_review_pack_csv(
    report: dict[str, Any],
    out_path: Path,
) -> dict[str, Any]:
    fieldnames = [
        "schema",
        "sequence",
        "recommendation_bucket",
        "recommendation_rule",
        "concept_id",
        "concept_type",
        "concept_name",
        "normalized_key",
        "definition_status",
        "review_status",
        "major_code",
        "major_name",
        "middle_code",
        "middle_name",
        "small_code",
        "small_name",
        "sub_code",
        "sub_name",
        "source_scope_key",
        "sample_unit_code",
        "sample_unit_name",
        "sample_element_name",
        "sample_ksa_text",
        "sample_atomic_text",
        "sample_criteria_text",
        "ksa_link_count",
        "atomic_link_count",
        "criteria_link_count",
        "operator_action",
        "is_button_review_row",
        "status_update_allowed",
        "db_writes",
        "approval_claim",
        "human_reviewed_written_by_report",
        "decision",
        "reviewer_id",
        "reviewed_at",
        "rationale",
    ]
    rows = report.get("rows") or []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            csv_row: dict[str, Any] = {"schema": LABEL_MISSING_GAP_CSV_SCHEMA}
            for field in fieldnames:
                if field == "schema":
                    continue
                value = "" if field in {"decision", "reviewer_id", "reviewed_at", "rationale"} else row.get(field, "")
                if isinstance(value, list):
                    value = ";".join(str(item) for item in value)
                csv_row[field] = _csv_safe_cell(value)
            writer.writerow(csv_row)
    return {
        "path": str(out_path),
        "record_count": len(rows),
        "schema": LABEL_MISSING_GAP_CSV_SCHEMA,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_reviewed_written_by_report": False,
    }
