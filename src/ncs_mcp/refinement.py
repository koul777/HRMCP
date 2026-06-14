from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from ncs_mcp.config import load_settings
from ncs_mcp.db import clamp_limit, connect, initialize_database, now_utc, row_to_dict


PROMPT_VERSION = "ncs-refinement-v1"
LOCAL_MODEL_NAME = "local-rule-refiner-v1"
ACTIONABLE_ISSUE_TYPES = [
    "double_space",
    "criteria_format_issue",
    "suspected_typo",
    "api_value_mismatch",
    "api_element_value_mismatch",
]
SUPPORTED_TARGET_TYPES = {"classification", "unit", "element", "criteria", "ksa"}


def content_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def ensure_sentence_period(text: str) -> str:
    cleaned = normalize_whitespace(text)
    if not cleaned:
        return cleaned
    if cleaned[-1] in ".!?。？！":
        return cleaned
    return cleaned + "."


def get_target_record(conn: sqlite3.Connection, target_type: str, target_id: str) -> dict[str, Any] | None:
    if target_type == "classification":
        row = conn.execute(
            """
            SELECT classification_id AS target_id, 'classification' AS target_type,
                   sub_name AS title_raw, duty_def_api AS body_raw,
                   duty_def_refined AS body_refined, review_status
            FROM classifications
            WHERE classification_id = ?
            """,
            (target_id,),
        ).fetchone()
    elif target_type == "unit":
        row = conn.execute(
            """
            SELECT cu.unit_code AS target_id, 'unit' AS target_type,
                   cu.unit_name_raw AS title_raw, cu.unit_name_refined AS title_refined,
                   cu.api_definition AS body_raw, cu.api_definition_refined AS body_refined,
                   cu.review_status,
                   c.major_name, c.middle_name, c.small_name, c.sub_name
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE cu.unit_code = ?
            """,
            (target_id,),
        ).fetchone()
    elif target_type == "element":
        row = conn.execute(
            """
            SELECT ce.element_id AS target_id, 'element' AS target_type,
                   ce.element_name_raw AS title_raw, ce.element_name_refined AS title_refined,
                   ce.api_element_name AS body_raw, NULL AS body_refined,
                   ce.review_status,
                   ce.unit_code, cu.unit_name_raw AS unit_name
            FROM competency_elements ce
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE ce.element_id = ?
            """,
            (target_id,),
        ).fetchone()
    elif target_type == "criteria":
        row = conn.execute(
            """
            SELECT pc.criteria_id AS target_id, 'criteria' AS target_type,
                   '수행준거 ' || pc.criteria_no AS title_raw,
                   pc.criteria_text_raw AS body_raw,
                   pc.criteria_text_refined AS body_refined,
                   pc.review_status,
                   ce.element_id, ce.element_name_raw AS element_name,
                   ce.unit_code, cu.unit_name_raw AS unit_name
            FROM performance_criteria pc
            JOIN competency_elements ce ON ce.element_id = pc.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE pc.criteria_id = ?
            """,
            (target_id,),
        ).fetchone()
    elif target_type == "ksa":
        row = conn.execute(
            """
            SELECT ki.ksa_id AS target_id, 'ksa' AS target_type,
                   ki.ksa_type_name || ' ' || ki.ksa_no AS title_raw,
                   ki.ksa_text_raw AS body_raw,
                   ki.ksa_text_refined AS body_refined,
                   ki.review_status,
                   ce.element_id, ce.element_name_raw AS element_name,
                   ce.unit_code, cu.unit_name_raw AS unit_name
            FROM ksa_items ki
            JOIN competency_elements ce ON ce.element_id = ki.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE ki.ksa_id = ?
            """,
            (target_id,),
        ).fetchone()
    else:
        return None
    return row_to_dict(row) if row else None


def raw_text_for_target(target: dict[str, Any]) -> str:
    body = target.get("body_raw") or ""
    if body:
        return str(body)
    return str(target.get("title_raw") or "")


def build_refinement_prompt(issue: dict[str, Any], target: dict[str, Any]) -> str:
    context = {
        "target_type": target["target_type"],
        "target_id": target["target_id"],
        "title": target.get("title_raw"),
        "raw_text": raw_text_for_target(target),
        "issue_type": issue["issue_type"],
        "issue_detail": issue["issue_detail"],
        "suggested_action": issue.get("suggested_action"),
        "unit_name": target.get("unit_name"),
        "element_name": target.get("element_name"),
    }
    return (
        "NCS 원문을 보존하면서 정제 후보를 만든다. 의미를 추측해 추가하지 말고, "
        "명백한 띄어쓰기/문장부호/깨진 표기만 보정한다. "
        "불확실하면 action='needs_human_review'로 둔다.\n\n"
        + json.dumps(context, ensure_ascii=False, indent=2)
    )


def local_rule_refine(issue: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    issue_type = str(issue["issue_type"])
    raw_text = raw_text_for_target(target)
    normalized = normalize_whitespace(raw_text)
    refined = normalized
    action = "keep"
    confidence = 0.5
    rationale = "원문을 변경할 충분한 근거가 없어 유지했다."
    warnings: list[str] = []

    if issue_type == "double_space":
        action = "refine" if normalized != raw_text else "keep"
        confidence = 0.99
        rationale = "연속 공백을 단일 공백으로 정규화했다."
    elif issue_type == "criteria_format_issue" and target["target_type"] == "criteria":
        with_period = ensure_sentence_period(normalized)
        if with_period != raw_text:
            refined = with_period
            action = "refine"
            confidence = 0.95
            rationale = "수행준거 문장의 끝 문장부호를 보완했다."
        else:
            action = "keep"
            confidence = 0.65
            rationale = "수행준거 형식 이슈가 감지됐지만 기계적으로 보정할 변경점은 없었다."
    elif issue_type in {"short_ksa", "duplicate_text"}:
        action = "needs_human_review"
        confidence = 0.35
        rationale = "짧거나 반복되는 KSA는 도메인 판단이 필요하므로 자동 정제하지 않았다."
    elif issue_type in {"suspected_typo", "api_value_mismatch", "api_element_value_mismatch"}:
        action = "needs_human_review"
        confidence = 0.4
        rationale = "오탈자/API 값 불일치는 원천 확인 또는 LLM+사람 검토가 필요하다."
        warnings.append("자동 수정 금지: 원문과 API 보강값을 함께 검토해야 한다.")
    else:
        action = "needs_human_review"
        confidence = 0.3
        rationale = "지원하지 않는 이슈 유형이라 자동 정제하지 않았다."

    return {
        "action": action,
        "raw_text": raw_text,
        "refined_text": refined,
        "rationale": rationale,
        "confidence": confidence,
        "warnings": warnings,
        "provider": LOCAL_MODEL_NAME,
        "prompt_version": PROMPT_VERSION,
    }


def select_quality_issues(
    conn: sqlite3.Connection,
    *,
    issue_types: list[str] | None = None,
    target_types: list[str] | None = None,
    severity: str | None = None,
    unresolved_only: bool = True,
    exclude_active_jobs: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    selected_issue_types = issue_types or ACTIONABLE_ISSUE_TYPES
    if selected_issue_types:
        placeholders = ",".join("?" for _ in selected_issue_types)
        clauses.append(f"issue_type IN ({placeholders})")
        params.extend(selected_issue_types)
    if target_types:
        placeholders = ",".join("?" for _ in target_types)
        clauses.append(f"target_type IN ({placeholders})")
        params.extend(target_types)
    else:
        placeholders = ",".join("?" for _ in SUPPORTED_TARGET_TYPES)
        clauses.append(f"target_type IN ({placeholders})")
        params.extend(sorted(SUPPORTED_TARGET_TYPES))
    if severity:
        clauses.append("severity = ?")
        params.append(severity)
    if unresolved_only:
        clauses.append("resolved_at IS NULL")
    if exclude_active_jobs:
        clauses.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM refinement_jobs r
                WHERE r.source_issue_id = quality_issues.issue_id
                  AND r.review_status IN ('review_required', 'accepted', 'applied')
            )
            """
        )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT *
        FROM quality_issues
        {where}
        ORDER BY
            CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
            issue_id
        LIMIT ?
        """,
        params + [clamp_limit(limit, default=50, maximum=5000)],
    ).fetchall()
    return [dict(row) for row in rows]


def refinement_job_exists(conn: sqlite3.Connection, input_hash: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM refinement_jobs
        WHERE input_hash = ?
          AND review_status IN ('review_required', 'accepted', 'applied')
        LIMIT 1
        """,
        (input_hash,),
    ).fetchone()
    return row is not None


def create_refinement_jobs(
    conn: sqlite3.Connection,
    *,
    issue_types: list[str] | None = None,
    target_types: list[str] | None = None,
    severity: str | None = None,
    provider: str = "local-rule",
    limit: int = 50,
    dry_run: bool = False,
) -> dict[str, Any]:
    if provider != "local-rule":
        raise ValueError("Only provider='local-rule' is implemented. Use refinement_jobs as the LLM handoff queue.")
    issues = select_quality_issues(
        conn,
        issue_types=issue_types,
        target_types=target_types,
        severity=severity,
        exclude_active_jobs=True,
        limit=limit,
    )
    created = 0
    skipped = 0
    missing_targets = 0
    previews: list[dict[str, Any]] = []
    for issue in issues:
        target = get_target_record(conn, issue["target_type"], str(issue["target_id"]))
        if target is None:
            missing_targets += 1
            continue
        prompt = build_refinement_prompt(issue, target)
        output = local_rule_refine(issue, target)
        input_payload = {
            "issue_id": issue["issue_id"],
            "target_type": issue["target_type"],
            "target_id": str(issue["target_id"]),
            "raw_text": raw_text_for_target(target),
            "issue_type": issue["issue_type"],
            "prompt_version": PROMPT_VERSION,
        }
        digest = content_hash(input_payload)
        if refinement_job_exists(conn, digest):
            skipped += 1
            continue
        output_text = json.dumps(
            {
                "prompt": prompt,
                "result": output,
                "issue": issue,
                "target": target,
            },
            ensure_ascii=False,
            indent=2,
        )
        previews.append(
            {
                "issue_id": issue["issue_id"],
                "target_type": issue["target_type"],
                "target_id": str(issue["target_id"]),
                "action": output["action"],
                "confidence": output["confidence"],
                "raw_text": output["raw_text"],
                "refined_text": output["refined_text"],
            }
        )
        if dry_run:
            continue
        conn.execute(
            """
            INSERT INTO refinement_jobs(
                target_type, target_id, source_issue_id, model_name, prompt_version,
                input_hash, raw_text, refined_text, rationale, confidence,
                output_text, review_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                issue["target_type"],
                str(issue["target_id"]),
                issue["issue_id"],
                LOCAL_MODEL_NAME,
                PROMPT_VERSION,
                digest,
                output["raw_text"],
                output["refined_text"],
                output["rationale"],
                output["confidence"],
                output_text,
                "review_required",
                now_utc(),
            ),
        )
        created += 1
    if not dry_run:
        conn.commit()
    return {
        "provider": provider,
        "issue_types": issue_types or ACTIONABLE_ISSUE_TYPES,
        "target_types": target_types or sorted(SUPPORTED_TARGET_TYPES),
        "issues_seen": len(issues),
        "jobs_created": created,
        "jobs_skipped_existing": skipped,
        "missing_targets": missing_targets,
        "dry_run": dry_run,
        "previews": previews[:20],
    }


def parse_job_result(row: sqlite3.Row) -> dict[str, Any]:
    if row["output_text"]:
        try:
            return json.loads(row["output_text"]).get("result", {})
        except json.JSONDecodeError:
            return {}
    return {}


def apply_refinement_to_target(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str,
    refined_text: str,
    review_status: str,
) -> None:
    if target_type == "classification":
        conn.execute(
            """
            UPDATE classifications
            SET duty_def_refined = ?, review_status = ?
            WHERE classification_id = ?
            """,
            (refined_text, review_status, target_id),
        )
    elif target_type == "unit":
        conn.execute(
            """
            UPDATE competency_units
            SET api_definition_refined = ?, review_status = ?, updated_at = ?
            WHERE unit_code = ?
            """,
            (refined_text, review_status, now_utc(), target_id),
        )
    elif target_type == "element":
        conn.execute(
            """
            UPDATE competency_elements
            SET element_name_refined = ?, review_status = ?
            WHERE element_id = ?
            """,
            (refined_text, review_status, target_id),
        )
    elif target_type == "criteria":
        conn.execute(
            """
            UPDATE performance_criteria
            SET criteria_text_refined = ?, review_status = ?
            WHERE criteria_id = ?
            """,
            (refined_text, review_status, target_id),
        )
    elif target_type == "ksa":
        conn.execute(
            """
            UPDATE ksa_items
            SET ksa_text_refined = ?, review_status = ?
            WHERE ksa_id = ?
            """,
            (refined_text, review_status, target_id),
        )
    else:
        raise ValueError(f"unsupported target_type: {target_type}")


def apply_refinement_jobs(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    min_confidence: float = 0.95,
    target_types: list[str] | None = None,
    job_status: str = "review_required",
    target_review_status: str = "model_refined",
    resolve_issues: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    clauses = ["review_status = ?", "confidence >= ?"]
    params: list[Any] = [job_status, min_confidence]
    if target_types:
        placeholders = ",".join("?" for _ in target_types)
        clauses.append(f"target_type IN ({placeholders})")
        params.extend(target_types)
    rows = conn.execute(
        f"""
        SELECT *
        FROM refinement_jobs
        WHERE {' AND '.join(clauses)}
        ORDER BY confidence DESC, job_id
        LIMIT ?
        """,
        params + [clamp_limit(limit, default=50, maximum=5000)],
    ).fetchall()
    applied = 0
    skipped = 0
    previews: list[dict[str, Any]] = []
    for row in rows:
        result = parse_job_result(row)
        if result.get("action") != "refine":
            skipped += 1
            continue
        refined_text = str(row["refined_text"] or result.get("refined_text") or "").strip()
        raw_text = str(row["raw_text"] or result.get("raw_text") or "").strip()
        if not refined_text or refined_text == raw_text:
            skipped += 1
            continue
        previews.append(
            {
                "job_id": row["job_id"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "confidence": row["confidence"],
                "raw_text": raw_text,
                "refined_text": refined_text,
            }
        )
        if dry_run:
            continue
        apply_refinement_to_target(
            conn,
            target_type=row["target_type"],
            target_id=row["target_id"],
            refined_text=refined_text,
            review_status=target_review_status,
        )
        conn.execute(
            "UPDATE refinement_jobs SET review_status = 'applied', applied_at = ? WHERE job_id = ?",
            (now_utc(), row["job_id"]),
        )
        if resolve_issues and row["source_issue_id"] is not None:
            conn.execute(
                "UPDATE quality_issues SET resolved_at = ? WHERE issue_id = ?",
                (now_utc(), row["source_issue_id"]),
            )
        applied += 1
    if not dry_run:
        conn.commit()
    return {
        "jobs_seen": len(rows),
        "jobs_applied": applied,
        "jobs_skipped": skipped,
        "min_confidence": min_confidence,
        "target_review_status": target_review_status,
        "dry_run": dry_run,
        "previews": previews[:20],
    }


def refinement_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def scalar(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    job_status = {
        row["review_status"]: row["count"]
        for row in conn.execute(
            """
            SELECT review_status, COUNT(*) AS count
            FROM refinement_jobs
            GROUP BY review_status
            ORDER BY review_status
            """
        ).fetchall()
    }
    target_status = {
        "classifications_refined": scalar(
            """
            SELECT COUNT(*)
            FROM classifications
            WHERE duty_def_refined IS NOT NULL AND TRIM(duty_def_refined) != ''
            """
        ),
        "competency_units_refined": scalar(
            """
            SELECT COUNT(*)
            FROM competency_units
            WHERE unit_name_refined IS NOT NULL OR api_definition_refined IS NOT NULL
            """
        ),
        "competency_elements_refined": scalar(
            """
            SELECT COUNT(*)
            FROM competency_elements
            WHERE element_name_refined IS NOT NULL AND TRIM(element_name_refined) != ''
            """
        ),
        "criteria_refined": scalar(
            """
            SELECT COUNT(*)
            FROM performance_criteria
            WHERE criteria_text_refined IS NOT NULL AND TRIM(criteria_text_refined) != ''
            """
        ),
        "ksa_refined": scalar(
            """
            SELECT COUNT(*)
            FROM ksa_items
            WHERE ksa_text_refined IS NOT NULL AND TRIM(ksa_text_refined) != ''
            """
        ),
    }
    issue_counts = {
        row["issue_type"]: row["count"]
        for row in conn.execute(
            """
            SELECT issue_type, COUNT(*) AS count
            FROM quality_issues
            WHERE resolved_at IS NULL
            GROUP BY issue_type
            ORDER BY count DESC
            """
        ).fetchall()
    }
    return {
        "refinement_jobs": job_status,
        "refined_targets": target_status,
        "open_quality_issues": issue_counts,
    }


def export_refinement_jsonl(
    conn: sqlite3.Connection,
    *,
    out_path: Path,
    issue_types: list[str] | None = None,
    target_types: list[str] | None = None,
    severity: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    issues = select_quality_issues(
        conn,
        issue_types=issue_types,
        target_types=target_types,
        severity=severity,
        exclude_active_jobs=True,
        limit=limit,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    missing_targets = 0
    with out_path.open("w", encoding="utf-8") as handle:
        for issue in issues:
            target = get_target_record(conn, issue["target_type"], str(issue["target_id"]))
            if target is None:
                missing_targets += 1
                continue
            payload = {
                "custom_id": f"quality_issue:{issue['issue_id']}",
                "issue_id": issue["issue_id"],
                "target_type": issue["target_type"],
                "target_id": str(issue["target_id"]),
                "issue_type": issue["issue_type"],
                "raw_text": raw_text_for_target(target),
                "instruction": (
                    "NCS 원문 의미를 바꾸지 말고 명백한 오류만 정제한다. "
                    "불확실하면 action을 needs_human_review로 둔다."
                ),
                "safety_rule": "원문 필드를 덮어쓰지 않는다. 개인정보/API 키/로컬 경로를 추가하지 않는다.",
                "expected_result_schema": {
                    "action": "refine | keep | needs_human_review",
                    "refined_text": "string",
                    "rationale": "string",
                    "confidence": "0.0-1.0",
                },
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            written += 1
    return {
        "out": str(out_path),
        "issues_seen": len(issues),
        "records_written": written,
        "missing_targets": missing_targets,
    }


def import_refinement_jsonl(
    conn: sqlite3.Connection,
    *,
    input_path: Path,
    model_name: str = "jsonl-import",
) -> dict[str, Any]:
    imported = 0
    skipped = 0
    errors: list[dict[str, Any]] = []
    timestamp = now_utc()
    with input_path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append({"line": line_no, "error": f"invalid_json:{exc.msg}"})
                skipped += 1
                continue
            required = ["issue_id", "target_type", "target_id", "refined_text", "rationale", "confidence"]
            missing = [key for key in required if key not in payload]
            if missing:
                errors.append({"line": line_no, "error": "missing_fields", "fields": missing})
                skipped += 1
                continue
            try:
                confidence = float(payload["confidence"])
            except (TypeError, ValueError):
                errors.append({"line": line_no, "error": "invalid_confidence"})
                skipped += 1
                continue
            target = get_target_record(conn, payload["target_type"], str(payload["target_id"]))
            if target is None:
                errors.append({"line": line_no, "error": "target_not_found"})
                skipped += 1
                continue
            input_payload = {
                "issue_id": payload["issue_id"],
                "target_type": payload["target_type"],
                "target_id": str(payload["target_id"]),
                "raw_text": raw_text_for_target(target),
                "prompt_version": PROMPT_VERSION,
                "imported": True,
            }
            digest = content_hash(input_payload)
            if refinement_job_exists(conn, digest):
                skipped += 1
                continue
            result = {
                "action": payload.get("action", "needs_human_review"),
                "raw_text": raw_text_for_target(target),
                "refined_text": str(payload["refined_text"]),
                "rationale": str(payload["rationale"]),
                "confidence": confidence,
                "provider": model_name,
                "prompt_version": PROMPT_VERSION,
            }
            conn.execute(
                """
                INSERT INTO refinement_jobs(
                    target_type, target_id, source_issue_id, model_name, prompt_version,
                    input_hash, raw_text, refined_text, rationale, confidence,
                    output_text, review_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["target_type"],
                    str(payload["target_id"]),
                    int(payload["issue_id"]),
                    model_name,
                    PROMPT_VERSION,
                    digest,
                    result["raw_text"],
                    result["refined_text"],
                    result["rationale"],
                    confidence,
                    json.dumps({"result": result, "import": payload}, ensure_ascii=False, indent=2),
                    "review_required",
                    timestamp,
                ),
            )
            imported += 1
    conn.commit()
    return {
        "input": str(input_path),
        "jobs_imported": imported,
        "records_skipped": skipped,
        "errors": errors[:20],
        "error_count": len(errors),
    }


def run_refinement_harness(
    db_path: Path,
    *,
    action: str,
    issue_types: list[str] | None = None,
    target_types: list[str] | None = None,
    severity: str | None = None,
    provider: str = "local-rule",
    limit: int = 50,
    min_confidence: float = 0.95,
    dry_run: bool = False,
    out_path: Path | None = None,
    input_path: Path | None = None,
) -> dict[str, Any]:
    conn = connect(db_path)
    initialize_database(conn)
    try:
        if action == "generate":
            return create_refinement_jobs(
                conn,
                issue_types=issue_types,
                target_types=target_types,
                severity=severity,
                provider=provider,
                limit=limit,
                dry_run=dry_run,
            )
        if action == "apply":
            return apply_refinement_jobs(
                conn,
                limit=limit,
                min_confidence=min_confidence,
                target_types=target_types,
                dry_run=dry_run,
            )
        if action == "stats":
            return refinement_stats(conn)
        if action == "export-jsonl":
            if out_path is None:
                raise ValueError("out_path is required for export-jsonl")
            return export_refinement_jsonl(
                conn,
                out_path=out_path,
                issue_types=issue_types,
                target_types=target_types,
                severity=severity,
                limit=limit,
            )
        if action == "import-jsonl":
            if input_path is None:
                raise ValueError("input_path is required for import-jsonl")
            return import_refinement_jsonl(conn, input_path=input_path, model_name=provider)
        raise ValueError(f"unsupported action: {action}")
    finally:
        conn.close()


def parse_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_args() -> argparse.Namespace:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Generate/apply NCS refinement jobs.")
    parser.add_argument("action", choices=["generate", "apply", "stats", "export-jsonl", "import-jsonl"])
    parser.add_argument("--db-path", type=Path, default=settings.db_path)
    parser.add_argument("--issue-types", help="Comma separated issue types.")
    parser.add_argument("--target-types", help="Comma separated target types.")
    parser.add_argument("--severity")
    parser.add_argument("--provider", default="local-rule")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--min-confidence", type=float, default=0.95)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--input", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_refinement_harness(
        args.db_path,
        action=args.action,
        issue_types=parse_csv(args.issue_types),
        target_types=parse_csv(args.target_types),
        severity=args.severity,
        provider=args.provider,
        limit=args.limit,
        min_confidence=args.min_confidence,
        dry_run=args.dry_run,
        out_path=args.out,
        input_path=args.input,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
