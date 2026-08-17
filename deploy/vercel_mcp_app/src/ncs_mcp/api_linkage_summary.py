from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = "ncs_api_linkage_summary_v1"
NCS_MAJOR_DISPLAY_NAMES = {
    "01": "\uc0ac\uc5c5\uad00\ub9ac",
    "02": "\uacbd\uc601\u00b7\ud68c\uacc4\u00b7\uc0ac\ubb34",
    "03": "\uae08\uc735\u00b7\ubcf4\ud5d8",
    "04": "\uad50\uc721\u00b7\uc790\uc5f0\u00b7\uc0ac\ud68c\uacfc\ud559",
    "05": "\ubc95\ub960\u00b7\uacbd\ucc30\u00b7\uc18c\ubc29\u00b7\uad50\ub3c4\u00b7\uad6d\ubc29",
    "06": "\ubcf4\uac74\u00b7\uc758\ub8cc",
    "07": "\uc0ac\ud68c\ubcf5\uc9c0\u00b7\uc885\uad50",
    "08": "\ubb38\ud654\u00b7\uc608\uc220\u00b7\ub514\uc790\uc778\u00b7\ubc29\uc1a1",
    "09": "\uc6b4\uc804\u00b7\uc6b4\uc1a1",
    "10": "\uc601\uc5c5\ud310\ub9e4",
    "11": "\uacbd\ube44\u00b7\uccad\uc18c",
    "12": "\uc774\uc6a9\u00b7\uc219\ubc15\u00b7\uc5ec\ud589\u00b7\uc624\ub77d\u00b7\uc2a4\ud3ec\uce20",
    "13": "\uc74c\uc2dd\uc11c\ube44\uc2a4",
    "14": "\uac74\uc124",
    "15": "\uae30\uacc4",
    "16": "\uc7ac\ub8cc",
    "17": "\ud654\ud559\u00b7\ubc14\uc774\uc624",
    "18": "\uc12c\uc720\u00b7\uc758\ubcf5",
    "19": "\uc804\uae30\u00b7\uc804\uc790",
    "20": "\uc815\ubcf4\ud1b5\uc2e0",
    "21": "\uc2dd\ud488\uac00\uacf5",
    "22": "\uc778\uc1c4\u00b7\ubaa9\uc7ac\u00b7\uac00\uad6c\u00b7\uacf5\uc608",
    "23": "\ud658\uacbd\u00b7\uc5d0\ub108\uc9c0\u00b7\uc548\uc804",
    "24": "\ub18d\ub9bc\uc5b4\uc5c5",
}


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _missing_table_issue(table_name: str, surface: str) -> dict[str, Any]:
    return {
        "severity": "warning",
        "code": "missing_optional_table",
        "table": table_name,
        "surface": surface,
        "message": (
            f"Optional table {table_name} is missing; {surface} coverage is "
            "reported as zero for this read-only snapshot."
        ),
    }


def _has_hangul(value: str) -> bool:
    return any("\uac00" <= char <= "\ud7a3" for char in value)


def _major_display_name(major_code: str, raw_name: Any) -> str:
    text = str(raw_name or "").strip()
    if text and (text.isascii() or _has_hangul(text)):
        return text
    return NCS_MAJOR_DISPLAY_NAMES.get(str(major_code).zfill(2), text)


def _markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|")


def _major_name_for_report_item(item: dict[str, Any]) -> str:
    return _markdown_cell(item.get("major_name_label") or item.get("major_name"))


def _safe_next_actions() -> list[dict[str, Any]]:
    return [
        {
            "area": "report_only_recheck",
            "status": "safe_now",
            "guard_required": False,
            "command": (
                "python scripts\\ncs_harness.py api-linkage-summary "
                "--major-code <code> --out reports\\api_linkage_summary_<date>.json "
                "--markdown-out reports\\api_linkage_summary_<date>.md"
            ),
            "notes": "Read-only DB snapshot; no API calls or DB writes.",
        }
    ]


def _guarded_collection_candidates(summary: dict[str, Any], majors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if int(summary.get("element_api_remaining_targets") or 0) > 0:
        candidates.append(
            {
                "area": "element_api_recovery",
                "status": "guarded_only",
                "guard_required": True,
                "command": "python scripts\\checkpoint_ncs006_element_api_status.py",
                "blocked_when": "ncs006_checkpoint_api_call_not_allowed",
                "notes": (
                    "Use the checkpoint and watchdog state before starting any "
                    "element API recovery. Do not start duplicate collectors."
                ),
            }
        )
    if float(summary.get("qualification_collection_coverage") or 0) < 1.0:
        candidates.append(
            {
                "area": "qualification_collection",
                "status": "blocked_until_guard_allows_api",
                "guard_required": True,
                "command": (
                    "python scripts\\ncs_harness.py collect-qualification-items "
                    "--all-units --limit-units 100 --num-of-rows 50 --max-pages 1 "
                    "--request-delay 2 --max-retries 1 --retry-backoff-seconds 30 "
                    "--stop-after-rate-limit-errors 3 "
                    "--ncs006-checkpoint-path reports\\checkpoint_ncs006_element_api_status_<DATE>_current.json"
                ),
                "blocked_when": "agent queue status has blocked_safety or ncs006_checkpoint_api_call_not_allowed",
                "notes": (
                    "Qualification collection is a guarded API write path. Run "
                    "qualification-retry-hygiene and agent-queue-status first."
                ),
            }
        )
    qualification_targets = _qualification_collection_gap_targets(majors)
    for item in qualification_targets["majors"]:
        major_code = str(item.get("major_code") or "")
        candidates.append(
            {
                "area": "qualification_collection_major",
                "status": "operator_api_collection_candidate",
                "guard_required": True,
                "command": (
                    "python scripts\\ncs_harness.py collect-qualification-items "
                    f"--major-code {major_code} --num-of-rows 50 --max-pages 1 "
                    "--request-delay 2 --max-retries 1 --retry-backoff-seconds 30 "
                    "--stop-after-rate-limit-errors 3 "
                    "--ncs006-checkpoint-path reports\\checkpoint_ncs006_element_api_status_<DATE>_current.json"
                ),
                "major_code": major_code,
                "notes": (
                    "Qualification collection is a guarded API write path. This "
                    "report only identifies scoped collection candidates."
                ),
            }
        )
    training_targets = _coverage_gap_targets(majors, surface_key="training_courses")
    for item in training_targets["majors"]:
        major_code = str(item.get("major_code") or "")
        candidates.append(
            {
                "area": "training_course_links",
                "status": "operator_api_collection_candidate",
                "guard_required": True,
                "command": (
                    "python scripts\\ncs_harness.py collect-training-courses "
                    f"--major-code {major_code} --num-of-rows 500"
                ),
                "major_code": major_code,
                "notes": (
                    "Use only the listed major code for operational collection. "
                    "This report does not start API calls."
                ),
            }
        )
    job_base_targets = _coverage_gap_targets(majors, surface_key="job_base")
    for item in job_base_targets["majors"]:
        major_code = str(item.get("major_code") or "")
        candidates.append(
            {
                "area": "job_base_links",
                "status": "operator_api_collection_candidate",
                "guard_required": True,
                "command": (
                    "python scripts\\ncs_harness.py collect-job-base "
                    f"--major-code {major_code} --num-of-rows 500"
                ),
                "major_code": major_code,
                "notes": (
                    "Use only the listed major code for operational collection. "
                    "This report does not start API calls."
                ),
            }
        )
    return candidates


def _qualification_coverage_plan_hint(
    summary: dict[str, Any],
    *,
    selected_major_codes: list[str],
    target_ratio: float = 0.9,
    batch_size: int = 100,
) -> dict[str, Any]:
    total_unit_count = int(summary.get("unit_count") or 0)
    attempted_unit_count = int(summary.get("qualification_attempted_unit_count") or 0)
    target_attempted_unit_count = math.ceil(total_unit_count * target_ratio)
    additional_attempted_units_needed = max(0, target_attempted_unit_count - attempted_unit_count)
    estimated_batch_count = (
        math.ceil(additional_attempted_units_needed / batch_size)
        if additional_attempted_units_needed and batch_size > 0
        else 0
    )
    is_filtered_scope = bool(selected_major_codes)
    global_coverage_plan_command = (
        "python scripts\\ncs_harness.py qualification-coverage-plan "
        f"--target-ratio {target_ratio} --batch-size {batch_size} "
        "--ncs006-checkpoint-path reports\\checkpoint_ncs006_element_api_status_<DATE>_current.json "
        "--out reports\\qualification_collection_coverage_plan_<date>.json "
        "--markdown-out reports\\qualification_collection_coverage_plan_<date>.md "
        "--csv-out reports\\qualification_collection_coverage_plan_<date>.csv"
    )
    return {
        "scope": "selected_majors_report_only" if is_filtered_scope else "all_majors",
        "scope_major_codes": selected_major_codes,
        "coverage_plan_command_scope": "all_units",
        "coverage_plan_matches_summary_scope": not is_filtered_scope,
        "target_ratio": target_ratio,
        "batch_size": batch_size,
        "total_unit_count": total_unit_count,
        "attempted_unit_count": attempted_unit_count,
        "collection_coverage": summary.get("qualification_collection_coverage"),
        "target_attempted_unit_count": target_attempted_unit_count,
        "additional_attempted_units_needed": additional_attempted_units_needed,
        "estimated_batch_count": estimated_batch_count,
        "must_run_qualification_retry_hygiene_first": True,
        "guard_required": True,
        "operator_timing_required": True,
        "db_writes": False,
        "api_calls": False,
        "human_review_status_updates": False,
        "qualification_retry_hygiene_command": (
            "python scripts\\ncs_harness.py qualification-retry-hygiene "
            "--ncs006-checkpoint-path reports\\checkpoint_ncs006_element_api_status_<DATE>_current.json "
            "--out reports\\qualification_retry_hygiene_<date>.json "
            "--markdown-out reports\\qualification_retry_hygiene_<date>.md"
        ),
        "coverage_plan_command": None if is_filtered_scope else global_coverage_plan_command,
        "global_coverage_plan_command": global_coverage_plan_command,
        "notes": (
            "Filtered counts are diagnostic only. The qualification-coverage-plan "
            "command is currently all-units only, so run an unfiltered "
            "api-linkage-summary plus the global coverage plan before broad guarded "
            "qualification API collection."
            if is_filtered_scope
            else "Counts match the all-major api-linkage-summary scope. The "
            "coverage-plan command is report-only and should be generated before "
            "any guarded qualification API collection run."
        ),
    }


def _coverage_gap_targets(
    majors: list[dict[str, Any]],
    *,
    surface_key: str,
    threshold: float = 0.9,
) -> dict[str, Any]:
    target_majors: list[dict[str, Any]] = []
    for item in majors:
        surface = item.get(surface_key) or {}
        linked_unit_ratio = float(surface.get("linked_unit_ratio") or 0)
        if linked_unit_ratio >= threshold:
            continue
        raw_major_name = item.get("major_name")
        target_majors.append(
            {
                "major_code": str(item.get("major_code") or ""),
                "major_name": raw_major_name,
                "major_name_label": item.get("major_name_label") or raw_major_name,
                "unit_count": int(item.get("unit_count") or 0),
                "linked_unit_count": int(surface.get("linked_unit_count") or 0),
                "linked_unit_ratio": linked_unit_ratio,
            }
        )
    return {
        "threshold": threshold,
        "major_count": len(target_majors),
        "major_codes": [item["major_code"] for item in target_majors],
        "majors": target_majors,
    }


def _qualification_collection_gap_targets(
    majors: list[dict[str, Any]],
    *,
    threshold: float = 0.9,
) -> dict[str, Any]:
    target_majors: list[dict[str, Any]] = []
    for item in majors:
        surface = item.get("qualifications") or {}
        collection_coverage = float(surface.get("collection_coverage") or 0)
        if collection_coverage >= threshold:
            continue
        unit_count = int(item.get("unit_count") or 0)
        attempted_unit_count = int(surface.get("attempted_unit_count") or 0)
        raw_major_name = item.get("major_name")
        target_majors.append(
            {
                "major_code": str(item.get("major_code") or ""),
                "major_name": raw_major_name,
                "major_name_label": item.get("major_name_label") or raw_major_name,
                "unit_count": unit_count,
                "attempted_unit_count": attempted_unit_count,
                "remaining_unit_count": max(unit_count - attempted_unit_count, 0),
                "collection_coverage": collection_coverage,
            }
        )
    return {
        "threshold": threshold,
        "major_count": len(target_majors),
        "major_codes": [item["major_code"] for item in target_majors],
        "majors": target_majors,
    }


def _status_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    if not _table_exists(conn, "ncs_qualification_collection_status"):
        return {}
    rows = conn.execute(
        """
        SELECT c.major_code, qs.collection_status, COUNT(*) AS unit_count
        FROM ncs_qualification_collection_status qs
        JOIN competency_units cu ON cu.unit_code = qs.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        GROUP BY c.major_code, qs.collection_status
        """
    ).fetchall()
    counts: dict[str, dict[str, int]] = {}
    for major_code, status, unit_count in rows:
        counts.setdefault(str(major_code), {})[str(status)] = int(unit_count or 0)
    return counts


def build_api_linkage_summary(
    conn: sqlite3.Connection,
    *,
    major_codes: list[str] | None = None,
) -> dict[str, Any]:
    selected_major_codes = [str(code).zfill(2) for code in major_codes or [] if str(code).strip()]
    selected_major_set = set(selected_major_codes)
    optional_tables = {
        "ncs_training_course_unit_links": _table_exists(conn, "ncs_training_course_unit_links"),
        "ncs_unit_job_base_links": _table_exists(conn, "ncs_unit_job_base_links"),
        "ncs_unit_qualification_links": _table_exists(conn, "ncs_unit_qualification_links"),
        "ncs_qualification_collection_status": _table_exists(
            conn,
            "ncs_qualification_collection_status",
        ),
    }
    source_issues = [
        _missing_table_issue("ncs_training_course_unit_links", "training_courses")
        for exists in [optional_tables["ncs_training_course_unit_links"]]
        if not exists
    ]
    if not optional_tables["ncs_unit_job_base_links"]:
        source_issues.append(_missing_table_issue("ncs_unit_job_base_links", "job_base"))
    if not optional_tables["ncs_unit_qualification_links"]:
        source_issues.append(
            _missing_table_issue("ncs_unit_qualification_links", "qualification_links")
        )
    if not optional_tables["ncs_qualification_collection_status"]:
        source_issues.append(
            _missing_table_issue(
                "ncs_qualification_collection_status",
                "qualification_collection",
            )
        )
    base_rows = conn.execute(
        """
        SELECT
            c.major_code,
            MIN(c.major_name) AS major_name,
            COUNT(DISTINCT cu.unit_code) AS unit_count,
            SUM(CASE WHEN cu.api_match_status = 'matched' THEN 1 ELSE 0 END) AS unit_api_matched,
            SUM(CASE WHEN cu.api_match_status = 'api_failed' THEN 1 ELSE 0 END) AS unit_api_failed,
            SUM(CASE WHEN cu.api_match_status = 'not_collected' THEN 1 ELSE 0 END) AS unit_api_not_collected,
            SUM(CASE WHEN cu.api_match_status = 'no_data' THEN 1 ELSE 0 END) AS unit_api_no_data
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        GROUP BY c.major_code
        ORDER BY c.major_code
        """
    ).fetchall()
    by_major: dict[str, dict[str, Any]] = {}
    for row in base_rows:
        major_code = str(row[0])
        unit_count = int(row[2] or 0)
        unit_api_matched = int(row[3] or 0)
        by_major[major_code] = {
            "major_code": major_code,
            "major_name": row[1],
            "major_name_label": _major_display_name(major_code, row[1]),
            "unit_count": unit_count,
            "unit_api": {
                "matched": unit_api_matched,
                "api_failed": int(row[4] or 0),
                "not_collected": int(row[5] or 0),
                "no_data": int(row[6] or 0),
                "matched_ratio": _ratio(unit_api_matched, unit_count),
            },
            "element_api": {
                "total": 0,
                "matched": 0,
                "api_failed": 0,
                "not_collected": 0,
                "no_data": 0,
                "matched_ratio": None,
                "remaining_targets": 0,
            },
            "training_courses": {
                "linked_unit_count": 0,
                "linked_unit_ratio": _ratio(0, unit_count),
                "course_count": 0,
                "unit_link_count": 0,
            },
            "job_base": {
                "linked_unit_count": 0,
                "linked_unit_ratio": _ratio(0, unit_count),
                "link_count": 0,
            },
            "qualifications": {
                "attempted_unit_count": 0,
                "collection_coverage": _ratio(0, unit_count),
                "linked_unit_count": 0,
                "linked_unit_ratio": _ratio(0, unit_count),
                "link_count": 0,
                "collection_status_counts": {},
            },
        }

    element_rows = conn.execute(
        """
        SELECT
            c.major_code,
            COUNT(*) AS element_count,
            SUM(CASE WHEN ce.api_match_status = 'matched' THEN 1 ELSE 0 END) AS matched,
            SUM(CASE WHEN ce.api_match_status = 'api_failed' THEN 1 ELSE 0 END) AS api_failed,
            SUM(CASE WHEN ce.api_match_status = 'not_collected' THEN 1 ELSE 0 END) AS not_collected,
            SUM(CASE WHEN ce.api_match_status = 'no_data' THEN 1 ELSE 0 END) AS no_data
        FROM competency_elements ce
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        GROUP BY c.major_code
        """
    ).fetchall()
    for row in element_rows:
        major_code = str(row[0])
        item = by_major.get(major_code)
        if not item:
            continue
        total = int(row[1] or 0)
        matched = int(row[2] or 0)
        api_failed = int(row[3] or 0)
        not_collected = int(row[4] or 0)
        no_data = int(row[5] or 0)
        item["element_api"] = {
            "total": total,
            "matched": matched,
            "api_failed": api_failed,
            "not_collected": not_collected,
            "no_data": no_data,
            "matched_ratio": _ratio(matched, total),
            "remaining_targets": not_collected + api_failed,
        }

    training_rows = (
        conn.execute(
            """
            SELECT
                c.major_code,
                COUNT(DISTINCT l.unit_code) AS linked_unit_count,
                COUNT(DISTINCT l.training_course_id) AS course_count,
                COUNT(*) AS unit_link_count
            FROM ncs_training_course_unit_links l
            JOIN competency_units cu ON cu.unit_code = l.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            GROUP BY c.major_code
            """
        ).fetchall()
        if optional_tables["ncs_training_course_unit_links"]
        else []
    )
    for row in training_rows:
        item = by_major.get(str(row[0]))
        if not item:
            continue
        unit_count = int(item["unit_count"] or 0)
        linked_units = int(row[1] or 0)
        item["training_courses"] = {
            "linked_unit_count": linked_units,
            "linked_unit_ratio": _ratio(linked_units, unit_count),
            "course_count": int(row[2] or 0),
            "unit_link_count": int(row[3] or 0),
        }

    job_base_rows = (
        conn.execute(
            """
            SELECT
                c.major_code,
                COUNT(DISTINCT l.unit_code) AS linked_unit_count,
                COUNT(*) AS link_count
            FROM ncs_unit_job_base_links l
            JOIN competency_units cu ON cu.unit_code = l.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            GROUP BY c.major_code
            """
        ).fetchall()
        if optional_tables["ncs_unit_job_base_links"]
        else []
    )
    for row in job_base_rows:
        item = by_major.get(str(row[0]))
        if not item:
            continue
        unit_count = int(item["unit_count"] or 0)
        linked_units = int(row[1] or 0)
        item["job_base"] = {
            "linked_unit_count": linked_units,
            "linked_unit_ratio": _ratio(linked_units, unit_count),
            "link_count": int(row[2] or 0),
        }

    qualification_link_rows = (
        conn.execute(
            """
            SELECT
                c.major_code,
                COUNT(DISTINCT l.unit_code) AS linked_unit_count,
                COUNT(*) AS link_count
            FROM ncs_unit_qualification_links l
            JOIN competency_units cu ON cu.unit_code = l.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            GROUP BY c.major_code
            """
        ).fetchall()
        if optional_tables["ncs_unit_qualification_links"]
        else []
    )
    for row in qualification_link_rows:
        item = by_major.get(str(row[0]))
        if not item:
            continue
        unit_count = int(item["unit_count"] or 0)
        linked_units = int(row[1] or 0)
        item["qualifications"].update(
            {
                "linked_unit_count": linked_units,
                "linked_unit_ratio": _ratio(linked_units, unit_count),
                "link_count": int(row[2] or 0),
            }
        )

    for major_code, counts in _status_counts(conn).items():
        item = by_major.get(major_code)
        if not item:
            continue
        attempted = sum(int(value) for value in counts.values())
        unit_count = int(item["unit_count"] or 0)
        item["qualifications"].update(
            {
                "attempted_unit_count": attempted,
                "collection_coverage": _ratio(attempted, unit_count),
                "collection_status_counts": dict(sorted(counts.items())),
            }
        )

    majors = [
        item
        for item in by_major.values()
        if not selected_major_set or str(item.get("major_code")) in selected_major_set
    ]
    missing_selected_major_codes = sorted(selected_major_set - {str(item.get("major_code")) for item in majors})
    summary = {
        "major_count": len(majors),
        "unit_count": sum(int(item["unit_count"] or 0) for item in majors),
        "element_count": sum(int(item["element_api"]["total"] or 0) for item in majors),
        "element_api_matched": sum(int(item["element_api"]["matched"] or 0) for item in majors),
        "element_api_remaining_targets": sum(
            int(item["element_api"]["remaining_targets"] or 0) for item in majors
        ),
        "training_linked_unit_count": sum(
            int(item["training_courses"]["linked_unit_count"] or 0) for item in majors
        ),
        "job_base_linked_unit_count": sum(
            int(item["job_base"]["linked_unit_count"] or 0) for item in majors
        ),
        "qualification_attempted_unit_count": sum(
            int(item["qualifications"]["attempted_unit_count"] or 0) for item in majors
        ),
        "qualification_linked_unit_count": sum(
            int(item["qualifications"]["linked_unit_count"] or 0) for item in majors
        ),
    }
    summary["element_api_matched_ratio"] = _ratio(
        int(summary["element_api_matched"]),
        int(summary["element_count"]),
    )
    summary["training_unit_coverage"] = _ratio(
        int(summary["training_linked_unit_count"]),
        int(summary["unit_count"]),
    )
    summary["job_base_unit_coverage"] = _ratio(
        int(summary["job_base_linked_unit_count"]),
        int(summary["unit_count"]),
    )
    summary["qualification_collection_coverage"] = _ratio(
        int(summary["qualification_attempted_unit_count"]),
        int(summary["unit_count"]),
    )
    summary["qualification_linked_unit_coverage"] = _ratio(
        int(summary["qualification_linked_unit_count"]),
        int(summary["unit_count"]),
    )
    diagnostic_targets = {
        "training_courses": _coverage_gap_targets(majors, surface_key="training_courses"),
        "job_base": _coverage_gap_targets(majors, surface_key="job_base"),
        "qualification_collection": _qualification_collection_gap_targets(majors),
    }
    qualification_coverage_plan_hint = _qualification_coverage_plan_hint(
        summary,
        selected_major_codes=selected_major_codes,
    )
    safe_next_actions = _safe_next_actions()
    guarded_collection_candidates = _guarded_collection_candidates(summary, majors)

    return {
        "ok": True,
        "schema": SCHEMA,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "human_review_status_updates": False,
        "sqf_active_scoring_source": False,
        "approval_claim": False,
        "human_decision_required": False,
        "safe_next_action_count": len(safe_next_actions),
        "guarded_collection_candidate_count": len(guarded_collection_candidates),
        "unguarded_collection_candidate_count": sum(
            1
            for action in guarded_collection_candidates
            if isinstance(action, dict) and action.get("guard_required") is not True
        ),
        "unsafe_safe_next_action_count": sum(
            1
            for action in safe_next_actions
            if isinstance(action, dict) and action.get("guard_required") is not False
        ),
        "summary": summary,
        "qualification_coverage_plan_hint": qualification_coverage_plan_hint,
        "diagnostic_targets": diagnostic_targets,
        "by_major": majors,
        "safe_next_actions": safe_next_actions,
        "guarded_collection_candidates": guarded_collection_candidates,
        "source_issues": source_issues,
        "missing_optional_tables": [
            table for table, exists in optional_tables.items() if not exists
        ],
        "optional_table_status": optional_tables,
        "filter": {
            "major_codes": selected_major_codes,
            "missing_major_codes": missing_selected_major_codes,
        },
        "policy": {
            "db_writes": False,
            "api_calls": False,
            "human_review_status_updates": False,
            "sqf_active_scoring_source": False,
        },
    }


def _percent(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def write_api_linkage_summary_markdown(report: dict[str, Any], out_path: Path) -> None:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    filter_info = report.get("filter") if isinstance(report.get("filter"), dict) else {}
    diagnostic_targets = report.get("diagnostic_targets") if isinstance(report.get("diagnostic_targets"), dict) else {}
    coverage_plan_hint = (
        report.get("qualification_coverage_plan_hint")
        if isinstance(report.get("qualification_coverage_plan_hint"), dict)
        else {}
    )
    major_codes = filter_info.get("major_codes") or []
    missing_major_codes = filter_info.get("missing_major_codes") or []
    lines = [
        "# NCS API Linkage Summary",
        "",
        "Report-only snapshot. No API calls, DB writes, human-review status updates, or SQF scoring use.",
        "",
        f"- major_code_filter: {', '.join(major_codes) if major_codes else 'all'}",
        f"- missing_major_codes: {', '.join(missing_major_codes) if missing_major_codes else '-'}",
        "",
        "## Summary",
        "",
        f"- major_count: {summary.get('major_count')}",
        f"- unit_count: {summary.get('unit_count')}",
        f"- element_api_matched_ratio: {_percent(summary.get('element_api_matched_ratio'))}",
        f"- element_api_remaining_targets: {summary.get('element_api_remaining_targets')}",
        f"- training_unit_coverage: {_percent(summary.get('training_unit_coverage'))}",
        f"- job_base_unit_coverage: {_percent(summary.get('job_base_unit_coverage'))}",
        f"- qualification_collection_coverage: {_percent(summary.get('qualification_collection_coverage'))}",
        f"- qualification_linked_unit_coverage: {_percent(summary.get('qualification_linked_unit_coverage'))}",
        "",
        "## Qualification Coverage Plan Hint",
        "",
        f"- scope: {coverage_plan_hint.get('scope') or '-'}",
        f"- target_ratio: {coverage_plan_hint.get('target_ratio')}",
        f"- batch_size: {coverage_plan_hint.get('batch_size')}",
        f"- attempted_units: {coverage_plan_hint.get('attempted_unit_count')} / {coverage_plan_hint.get('total_unit_count')}",
        f"- additional_attempted_units_needed: {coverage_plan_hint.get('additional_attempted_units_needed')}",
        f"- estimated_batch_count: {coverage_plan_hint.get('estimated_batch_count')}",
        f"- coverage_plan_command_scope: {coverage_plan_hint.get('coverage_plan_command_scope') or '-'}",
        f"- coverage_plan_matches_summary_scope: {str(coverage_plan_hint.get('coverage_plan_matches_summary_scope')).lower()}",
        f"- must_run_qualification_retry_hygiene_first: {str(coverage_plan_hint.get('must_run_qualification_retry_hygiene_first')).lower()}",
        f"- qualification_retry_hygiene_command: `{coverage_plan_hint.get('qualification_retry_hygiene_command') or ''}`",
        "- coverage_plan_command: "
        f"`{coverage_plan_hint.get('coverage_plan_command') or 'not_available_for_filtered_scope'}`",
        f"- global_coverage_plan_command: `{coverage_plan_hint.get('global_coverage_plan_command') or ''}`",
        f"- notes: {coverage_plan_hint.get('notes') or '-'}",
        "",
        "## Diagnostic Targets",
        "",
        "Guarded recovery commands are listed under Guarded Collection Candidates.",
        "",
    ]
    for surface_key, surface_title in (("training_courses", "Training Courses"), ("job_base", "Job Base")):
        surface = diagnostic_targets.get(surface_key) if isinstance(diagnostic_targets, dict) else {}
        lines.extend(
            [
                f"### {surface_title}",
                "",
                f"- threshold: {surface.get('threshold')}",
                f"- major_count: {surface.get('major_count')}",
                f"- major_codes: {', '.join(surface.get('major_codes') or []) if surface.get('major_codes') else '-'}",
            ]
        )
        majors = surface.get("majors") if isinstance(surface, dict) else []
        if majors:
            lines.extend(["", "| Major | Name | Linked Units | Unit Count | Coverage |", "|---|---:|---:|---:|---:|"])
            for item in majors:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    "| {major} | {name} | {linked} | {units} | {coverage} |".format(
                        major=item.get("major_code"),
                        name=_major_name_for_report_item(item),
                        linked=item.get("linked_unit_count"),
                        units=item.get("unit_count"),
                        coverage=_percent(item.get("linked_unit_ratio")),
                    )
                )
        lines.extend([""])
    qualification_surface = (
        diagnostic_targets.get("qualification_collection") if isinstance(diagnostic_targets, dict) else {}
    )
    lines.extend(
        [
            "### Qualification Collection",
            "",
            f"- threshold: {qualification_surface.get('threshold')}",
            f"- major_count: {qualification_surface.get('major_count')}",
            "- major_codes: "
            f"{', '.join(qualification_surface.get('major_codes') or []) if qualification_surface.get('major_codes') else '-'}",
        ]
    )
    qualification_majors = qualification_surface.get("majors") if isinstance(qualification_surface, dict) else []
    if qualification_majors:
        lines.extend(
            [
                "",
                "| Major | Name | Attempted Units | Remaining Units | Unit Count | Coverage |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for item in qualification_majors:
            if not isinstance(item, dict):
                continue
            lines.append(
                "| {major} | {name} | {attempted} | {remaining} | {units} | {coverage} |".format(
                    major=item.get("major_code"),
                    name=_major_name_for_report_item(item),
                    attempted=item.get("attempted_unit_count"),
                    remaining=item.get("remaining_unit_count"),
                    units=item.get("unit_count"),
                    coverage=_percent(item.get("collection_coverage")),
                )
            )
    lines.extend([""])
    lines.extend(
        [
            "## By Major",
            "",
            "| Major | Name | Units | Element API | Remaining | Training Units | Job Base Units | Qualification Attempted | Qualification Units |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in report.get("by_major") or []:
        if not isinstance(item, dict):
            continue
        element = item.get("element_api") or {}
        training = item.get("training_courses") or {}
        job_base = item.get("job_base") or {}
        qualification = item.get("qualifications") or {}
        lines.append(
            "| {major} | {name} | {units} | {element} | {remaining} | {training} | {job_base} | {qual_attempted} | {qual_linked} |".format(
                major=item.get("major_code"),
                name=_major_name_for_report_item(item),
                units=item.get("unit_count"),
                element=_percent(element.get("matched_ratio")),
                remaining=element.get("remaining_targets"),
                training=f"{training.get('linked_unit_count')} ({_percent(training.get('linked_unit_ratio'))})",
                job_base=f"{job_base.get('linked_unit_count')} ({_percent(job_base.get('linked_unit_ratio'))})",
                qual_attempted=f"{qualification.get('attempted_unit_count')} ({_percent(qualification.get('collection_coverage'))})",
                qual_linked=f"{qualification.get('linked_unit_count')} ({_percent(qualification.get('linked_unit_ratio'))})",
            )
        )
    actions = report.get("safe_next_actions") if isinstance(report.get("safe_next_actions"), list) else []
    lines.extend(
        [
            "",
            "## Safe Next Actions",
            "",
            "| Area | Status | Guard | Command | Notes |",
            "|---|---:|---:|---|---|",
        ]
    )
    for action in actions:
        if not isinstance(action, dict):
            continue
        lines.append(
            "| {area} | {status} | {guard} | `{command}` | {notes} |".format(
                area=str(action.get("area") or "").replace("|", "\\|"),
                status=str(action.get("status") or "").replace("|", "\\|"),
                guard="yes" if action.get("guard_required") else "no",
                command=str(action.get("command") or "").replace("|", "\\|"),
                notes=str(action.get("notes") or "").replace("|", "\\|"),
            )
        )
    guarded_candidates = (
        report.get("guarded_collection_candidates")
        if isinstance(report.get("guarded_collection_candidates"), list)
        else []
    )
    lines.extend(
        [
            "",
            "## Guarded Collection Candidates",
            "",
            "| Area | Status | Guard | Command | Notes |",
            "|---|---:|---:|---|---|",
        ]
    )
    for candidate in guarded_candidates:
        if not isinstance(candidate, dict):
            continue
        lines.append(
            "| {area} | {status} | {guard} | `{command}` | {notes} |".format(
                area=str(candidate.get("area") or "").replace("|", "\\|"),
                status=str(candidate.get("status") or "").replace("|", "\\|"),
                guard="yes" if candidate.get("guard_required") else "no",
                command=str(candidate.get("command") or "").replace("|", "\\|"),
                notes=str(candidate.get("notes") or "").replace("|", "\\|"),
            )
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_api_linkage_summary_json(report: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
