from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


SQF_HUMAN_REVIEW_READINESS_SCHEMA = "ncs_sqf_human_review_readiness_v1"
FORMAT_VERSION = "ncs-sqf-human-review-readiness-v1"

REPORT_ONLY_GUARDRAILS = {
    "approval_ready": False,
    "db_writes": False,
    "status_update_allowed": False,
    "used_for_scoring": False,
    "approval_claim": False,
}

SENSITIVE_MARKERS = (
    "asset_path",
    "local_path",
    "db_path",
    "source_payload",
    "raw_payload",
    "raw_response",
)

SOURCE_LABELS = {
    "corpus_audit": "corpus audit",
    "claim_report": "claim candidates",
    "priority_report": "review priority",
    "decision_audit": "decision audit",
}


def build_sqf_human_review_readiness(
    corpus_audit_path: str | Path | None = None,
    claim_report_path: str | Path | None = None,
    priority_report_path: str | Path | None = None,
    decision_audit_path: str | Path | None = None,
    additional_artifact_paths: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Build a report-only SQF Human Review readiness summary from JSON artifacts."""
    findings: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}
    payloads = {
        "corpus_audit": _load_optional_report("corpus_audit", corpus_audit_path, findings, sources),
        "claim_report": _load_optional_report("claim_report", claim_report_path, findings, sources),
        "priority_report": _load_optional_report("priority_report", priority_report_path, findings, sources),
        "decision_audit": _load_optional_report("decision_audit", decision_audit_path, findings, sources),
    }

    source_issue_counts = {
        "input_not_ok_count": 0,
        "source_guardrail_issue_count": 0,
        "sensitive_reference_count": 0,
        "invalid_issue_count": 0,
    }
    for source_key, payload in payloads.items():
        if payload is None:
            continue

        report_ok = _top_level_ok(payload)
        if report_ok is False:
            source_issue_counts["input_not_ok_count"] += 1
            _append_finding(
                findings,
                source=source_key,
                severity="blocker",
                code="input_report_not_ok",
                message=f"The {SOURCE_LABELS[source_key]} artifact reports ok=false.",
            )

        guardrail_count = _input_guardrail_issue_count(payload)
        explicit_guardrail_count = _explicit_guardrail_issue_count(source_key, payload)
        guardrail_total = guardrail_count + explicit_guardrail_count
        if guardrail_total:
            source_issue_counts["source_guardrail_issue_count"] += guardrail_total
            _append_finding(
                findings,
                source=source_key,
                severity="blocker",
                code="source_guardrail_issue_detected",
                message=f"The {SOURCE_LABELS[source_key]} artifact contains report-only guardrail issues.",
                count=guardrail_total,
            )

        sensitive_count = _sensitive_reference_count(payload) + _explicit_sensitive_count(source_key, payload)
        if sensitive_count:
            source_issue_counts["sensitive_reference_count"] += sensitive_count
            _append_finding(
                findings,
                source=source_key,
                severity="blocker",
                code="sensitive_reference_detected",
                message=f"The {SOURCE_LABELS[source_key]} artifact contains internal-reference markers.",
                count=sensitive_count,
            )

        invalid_count = _explicit_invalid_count(source_key, payload)
        if invalid_count:
            source_issue_counts["invalid_issue_count"] += invalid_count
            _append_finding(
                findings,
                source=source_key,
                severity="blocker",
                code="invalid_review_issue_detected",
                message=f"The {SOURCE_LABELS[source_key]} artifact contains invalid review rows or records.",
                count=invalid_count,
            )

    additional_artifact_summaries = _audit_additional_artifacts(
        additional_artifact_paths or [],
        findings,
        sources,
    )
    if additional_artifact_summaries["missing_count"]:
        source_issue_counts["input_not_ok_count"] += additional_artifact_summaries["missing_count"]
    if additional_artifact_summaries["unreadable_count"]:
        source_issue_counts["input_not_ok_count"] += additional_artifact_summaries["unreadable_count"]
    if additional_artifact_summaries["sensitive_reference_count"]:
        source_issue_counts["sensitive_reference_count"] += additional_artifact_summaries[
            "sensitive_reference_count"
        ]

    summaries = {
        "corpus": _summarize_corpus(payloads["corpus_audit"]),
        "claim_queue": _summarize_claim_queue(payloads["claim_report"]),
        "priority": _summarize_priority(payloads["priority_report"]),
        "decision_audit": _summarize_decision_audit(payloads["decision_audit"]),
        "additional_artifacts": additional_artifact_summaries,
    }
    next_actions = _recommended_next_actions(summaries, source_issue_counts, payloads)
    ok = all(count == 0 for count in source_issue_counts.values()) and not any(
        finding.get("severity") == "blocker" for finding in findings
    )

    return {
        "ok": ok,
        "schema": SQF_HUMAN_REVIEW_READINESS_SCHEMA,
        "format_version": FORMAT_VERSION,
        "record_type": "sqf_human_review_readiness",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "review_required",
        **REPORT_ONLY_GUARDRAILS,
        "allowed_use": "supplementary_review_context_only",
        "review_policy": {
            "report_only": True,
            "requires_explicit_human_decision": True,
            "no_status_updates_performed": True,
            "guarded_import_required_for_status_change": True,
            "sqf_active_recommendation_scoring": False,
            **REPORT_ONLY_GUARDRAILS,
        },
        "sources": sources,
        "summaries": summaries,
        "source_issue_counts": source_issue_counts,
        "findings": findings,
        "next_actions": next_actions,
        "notes": [
            "This readiness summary is report-only and performs no DB writes.",
            "SQF evidence remains supplementary Human Review context, not active recommendation scoring evidence.",
            "A later guarded import requires explicit human decisions and a separate authorization step.",
        ],
    }


def write_sqf_human_review_readiness_json(report: dict[str, Any], out_path: str | Path) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def render_sqf_human_review_readiness_markdown(report: dict[str, Any]) -> str:
    summaries = report.get("summaries") or {}
    corpus = summaries.get("corpus") or {}
    claim_queue = summaries.get("claim_queue") or {}
    priority = summaries.get("priority") or {}
    decision = summaries.get("decision_audit") or {}
    issue_counts = report.get("source_issue_counts") or {}
    lines = [
        "# SQF Human Review Readiness",
        "",
        "## Contract",
        "",
        f"- ok: {str(report.get('ok')).lower()}",
        f"- schema: {_markdown_text(report.get('schema'))}",
        f"- format_version: {_markdown_text(report.get('format_version'))}",
        f"- status: {_markdown_text(report.get('status'))}",
        f"- approval_ready: {str(report.get('approval_ready')).lower()}",
        f"- db_writes: {str(report.get('db_writes')).lower()}",
        f"- status_update_allowed: {str(report.get('status_update_allowed')).lower()}",
        f"- used_for_scoring: {str(report.get('used_for_scoring')).lower()}",
        f"- approval_claim: {str(report.get('approval_claim')).lower()}",
        f"- allowed_use: {_markdown_text(report.get('allowed_use'))}",
        "",
        "## Source Artifacts",
        "",
    ]
    ordered_source_keys = [*SOURCE_LABELS, *sorted(key for key in (report.get("sources") or {}) if key not in SOURCE_LABELS)]
    for source_key in ordered_source_keys:
        source = (report.get("sources") or {}).get(source_key) or {}
        lines.append(
            "- "
            f"{source_key}: "
            f"provided={str(source.get('provided')).lower()} "
            f"loaded={str(source.get('loaded')).lower()} "
            f"name={_markdown_text(source.get('name') or '')}"
        )

    lines.extend(
        [
            "",
            "## Summaries",
            "",
            "### Corpus",
            "",
            f"- official_file_count: {corpus.get('official_file_count', 0)}",
            f"- downloaded_file_count: {corpus.get('downloaded_file_count', 0)}",
            f"- missing_official_downloaded_files: {corpus.get('missing_official_downloaded_files', 0)}",
            f"- chunk_count: {corpus.get('chunk_count', 0)}",
            f"- match_counts: {_json_inline(corpus.get('match_counts') or {})}",
            "",
            "### Claim Queue",
            "",
            f"- claim_count: {claim_queue.get('claim_count', 0)}",
            f"- human_review_required: {str(claim_queue.get('human_review_required')).lower()}",
            f"- job_counts: {_json_inline(claim_queue.get('job_counts') or {})}",
            f"- source_counts: {_json_inline(claim_queue.get('source_counts') or {})}",
            f"- mapping_relation_counts: {_json_inline(claim_queue.get('mapping_relation_counts') or {})}",
            "",
            "### Priority",
            "",
            f"- priority_counts: {_json_inline(priority.get('priority_counts') or {})}",
            f"- source_guardrail_issue_count: {priority.get('source_guardrail_issue_count', 0)}",
            "",
            "### Decision Audit",
            "",
            f"- row_count: {decision.get('row_count', 0)}",
            f"- pending_blank_count: {decision.get('pending_blank_count', 0)}",
            f"- completed_decision_count: {decision.get('completed_decision_count', 0)}",
            f"- invalid_count: {decision.get('invalid_count', 0)}",
            f"- import_ready_count: {decision.get('import_ready_count', 0)}",
            f"- sensitive_reference_count: {decision.get('sensitive_reference_count', 0)}",
            f"- guardrail_issue_count: {decision.get('guardrail_issue_count', 0)}",
            "",
            "### Additional Artifacts",
            "",
            f"- provided_count: {(summaries.get('additional_artifacts') or {}).get('provided_count', 0)}",
            f"- loaded_count: {(summaries.get('additional_artifacts') or {}).get('loaded_count', 0)}",
            f"- sensitive_reference_count: {(summaries.get('additional_artifacts') or {}).get('sensitive_reference_count', 0)}",
            "",
            "## Issue Counts",
            "",
            f"- input_not_ok_count: {issue_counts.get('input_not_ok_count', 0)}",
            f"- source_guardrail_issue_count: {issue_counts.get('source_guardrail_issue_count', 0)}",
            f"- sensitive_reference_count: {issue_counts.get('sensitive_reference_count', 0)}",
            f"- invalid_issue_count: {issue_counts.get('invalid_issue_count', 0)}",
        ]
    )

    findings = [finding for finding in report.get("findings") or [] if isinstance(finding, dict)]
    if findings:
        lines.extend(["", "## Findings", ""])
        for finding in findings:
            count = finding.get("count")
            count_text = f" count={count}" if count not in (None, "") else ""
            lines.append(
                "- "
                f"{_markdown_text(finding.get('severity'))} "
                f"{_markdown_text(finding.get('source'))} "
                f"{_markdown_text(finding.get('code'))}{count_text}: "
                f"{_markdown_text(finding.get('message'))}"
            )

    next_actions = [action for action in report.get("next_actions") or [] if action]
    if next_actions:
        lines.extend(["", "## Next Actions", ""])
        for action in next_actions:
            lines.append(f"- {_markdown_text(action)}")

    return "\n".join(lines).rstrip() + "\n"


def write_sqf_human_review_readiness_markdown(report: dict[str, Any], out_path: str | Path) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_sqf_human_review_readiness_markdown(report), encoding="utf-8")


def _load_optional_report(
    source_key: str,
    source_path: str | Path | None,
    findings: list[dict[str, Any]],
    sources: dict[str, dict[str, Any]],
) -> Any | None:
    if source_path is None:
        sources[source_key] = {"provided": False, "loaded": False, "name": None, "ok": None}
        _append_finding(
            findings,
            source=source_key,
            severity="info",
            code="input_not_provided",
            message=f"Optional {SOURCE_LABELS[source_key]} artifact was not provided.",
        )
        return None

    path = Path(source_path)
    name = _safe_artifact_name(path)
    if not path.exists():
        sources[source_key] = {"provided": True, "loaded": False, "name": name, "ok": None}
        _append_finding(
            findings,
            source=source_key,
            severity="warning",
            code="input_missing",
            message=f"Optional {SOURCE_LABELS[source_key]} artifact was not found.",
            artifact=name,
        )
        return None

    try:
        payload = _read_json_or_jsonl(path)
    except (OSError, ValueError, json.JSONDecodeError):
        sources[source_key] = {"provided": True, "loaded": False, "name": name, "ok": None}
        _append_finding(
            findings,
            source=source_key,
            severity="blocker",
            code="input_unreadable",
            message=f"The {SOURCE_LABELS[source_key]} artifact could not be loaded as JSON.",
            artifact=name,
        )
        return None

    sources[source_key] = {"provided": True, "loaded": True, "name": name, "ok": _top_level_ok(payload)}
    return payload


def _audit_additional_artifacts(
    artifact_paths: list[str | Path],
    findings: list[dict[str, Any]],
    sources: dict[str, dict[str, Any]],
) -> dict[str, int]:
    summary = {
        "provided_count": len(artifact_paths),
        "loaded_count": 0,
        "missing_count": 0,
        "unreadable_count": 0,
        "sensitive_reference_count": 0,
    }
    for index, artifact_path in enumerate(artifact_paths, start=1):
        source_key = f"additional_artifact_{index}"
        path = Path(artifact_path)
        name = _safe_artifact_name(path)
        if not path.exists():
            summary["missing_count"] += 1
            sources[source_key] = {"provided": True, "loaded": False, "name": name, "ok": False}
            _append_finding(
                findings,
                source=source_key,
                severity="blocker",
                code="additional_artifact_missing",
                message="An explicitly provided SQF bundle artifact was not found.",
                artifact=name,
            )
            continue
        try:
            payload = _read_artifact_for_sensitivity(path)
        except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            summary["unreadable_count"] += 1
            sources[source_key] = {"provided": True, "loaded": False, "name": name, "ok": False}
            _append_finding(
                findings,
                source=source_key,
                severity="blocker",
                code="additional_artifact_unreadable",
                message="An explicitly provided SQF bundle artifact could not be inspected.",
                artifact=name,
            )
            continue

        sensitive_count = _sensitive_reference_count(payload)
        summary["loaded_count"] += 1
        summary["sensitive_reference_count"] += sensitive_count
        sources[source_key] = {"provided": True, "loaded": True, "name": name, "ok": sensitive_count == 0}
        if sensitive_count:
            _append_finding(
                findings,
                source=source_key,
                severity="blocker",
                code="additional_artifact_sensitive_reference_detected",
                message="An explicitly provided SQF bundle artifact contains internal-reference markers.",
                count=sensitive_count,
                artifact=name,
            )
    return summary


def _read_artifact_for_sensitivity(path: Path) -> Any:
    suffix = path.suffix.casefold()
    if suffix in {".json", ".jsonl"}:
        return _read_json_or_jsonl(path)
    return {"artifact_text": path.read_text(encoding="utf-8-sig")}


def _read_json_or_jsonl(path: Path) -> Any:
    text = path.read_text(encoding="utf-8-sig")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
        if not records:
            raise ValueError("empty JSONL artifact")
        if isinstance(records[0], dict) and records[0].get("record_type") == "batch":
            return {"ok": True, "batch": records[0], "claims": [record for record in records[1:] if isinstance(record, dict)]}
        return records


def _summarize_corpus(payload: Any | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "official_file_count": 0,
            "downloaded_file_count": 0,
            "missing_official_downloaded_files": 0,
            "chunk_count": 0,
            "match_counts": {},
        }
    summary = payload.get("summary") or {}
    file_audit = payload.get("file_audit") or {}
    matching = payload.get("matching") or {}
    chunk_stats = _nested_get(payload, ("document_extraction", "chunk_stats")) or {}
    match_counts = _compact_counts(
        {
            "chunk_match_count": _first_int(
                payload.get("chunk_match_count"),
                summary.get("chunk_match_count"),
                _nested_get(matching, ("chunk_match_stats", "match_count")),
            ),
            "sqf_ncs_candidate_count": _first_int(
                payload.get("sqf_ncs_candidate_count"),
                summary.get("sqf_ncs_candidate_count"),
            ),
            "matched_chunk_count": _first_int(_nested_get(matching, ("chunk_match_stats", "matched_chunk_count"))),
            "matched_job_level_count": _first_int(
                _nested_get(matching, ("chunk_match_stats", "matched_job_level_count"))
            ),
            "matched_source_key_count": _first_int(
                _nested_get(matching, ("chunk_match_stats", "matched_source_key_count"))
            ),
        }
    )
    relation_counts = matching.get("sqf_ncs_relation_counts")
    chunk_relation_counts = matching.get("chunk_match_relation_counts")
    if isinstance(relation_counts, dict):
        match_counts["sqf_ncs_relation_counts"] = _int_count_dict(relation_counts)
    if isinstance(chunk_relation_counts, dict):
        match_counts["chunk_match_relation_counts"] = _int_count_dict(chunk_relation_counts)
    return {
        "official_file_count": _first_int(
            payload.get("official_file_count"),
            summary.get("official_file_count"),
            file_audit.get("official_file_count"),
            0,
        ),
        "downloaded_file_count": _first_int(
            payload.get("downloaded_file_count"),
            payload.get("official_downloaded_count"),
            summary.get("downloaded_file_count"),
            summary.get("official_downloaded_count"),
            file_audit.get("downloaded_file_count"),
            file_audit.get("official_downloaded_count"),
            0,
        ),
        "missing_official_downloaded_files": _first_int(
            payload.get("missing_official_downloaded_files"),
            _list_len(payload.get("missing_official_downloaded_files")),
            summary.get("missing_official_downloaded_files"),
            _list_len(summary.get("missing_official_downloaded_files")),
            _list_len(file_audit.get("missing_official_downloaded_files")),
            0,
        ),
        "chunk_count": _first_int(payload.get("chunk_count"), summary.get("chunk_count"), chunk_stats.get("chunk_count"), 0),
        "match_counts": match_counts,
    }


def _summarize_claim_queue(payload: Any | None) -> dict[str, Any]:
    claims = _claim_records(payload)
    batch = payload.get("batch") if isinstance(payload, dict) else {}
    batch = batch or {}
    summary = batch.get("summary") if isinstance(batch, dict) else {}
    summary = summary or (payload.get("summary") if isinstance(payload, dict) else {}) or {}
    relation_counts = _first_dict(
        summary.get("mapping_relation_counts"),
        summary.get("relation_counts"),
        _count_claim_relations(claims),
    )
    return {
        "claim_count": _first_int(
            payload.get("claim_count") if isinstance(payload, dict) else None,
            batch.get("claim_count") if isinstance(batch, dict) else None,
            summary.get("claim_count") if isinstance(summary, dict) else None,
            len(claims),
            0,
        ),
        "job_counts": _int_count_dict(_first_dict(summary.get("job_counts") if isinstance(summary, dict) else None, _count_claim_jobs(claims))),
        "source_counts": _int_count_dict(
            _first_dict(
                summary.get("source_counts") if isinstance(summary, dict) else None,
                summary.get("source_type_counts") if isinstance(summary, dict) else None,
                _count_claim_sources(claims),
            )
        ),
        "mapping_relation_counts": _int_count_dict(relation_counts),
        "human_review_required": True,
    }


def _summarize_priority(payload: Any | None) -> dict[str, Any]:
    summary = payload.get("summary") if isinstance(payload, dict) else {}
    summary = summary or {}
    priority_counts = summary.get("priority_counts") if isinstance(summary, dict) else None
    if not isinstance(priority_counts, dict):
        priority_counts = Counter()
        if isinstance(payload, dict):
            for item in payload.get("items") or []:
                if isinstance(item, dict):
                    priority = item.get("priority")
                    if priority:
                        priority_counts[str(priority)] += 1
    return {
        "priority_counts": _int_count_dict(priority_counts),
        "source_guardrail_issue_count": _first_int(
            payload.get("source_guardrail_issue_count") if isinstance(payload, dict) else None,
            summary.get("source_guardrail_issue_count") if isinstance(summary, dict) else None,
            _list_len(summary.get("source_guardrail_issues") if isinstance(summary, dict) else None),
            0,
        ),
    }


def _summarize_decision_audit(payload: Any | None) -> dict[str, Any]:
    summary = payload.get("summary") if isinstance(payload, dict) else {}
    summary = summary or {}
    row_count = _first_int(
        payload.get("row_count") if isinstance(payload, dict) else None,
        summary.get("row_count") if isinstance(summary, dict) else None,
        _list_len(payload.get("rows") if isinstance(payload, dict) else None),
        0,
    )
    pending_blank_count = _first_int(
        payload.get("pending_review_count") if isinstance(payload, dict) else None,
        payload.get("blank_count") if isinstance(payload, dict) else None,
        payload.get("pending_blank_count") if isinstance(payload, dict) else None,
        summary.get("pending_review_count") if isinstance(summary, dict) else None,
        summary.get("blank_count") if isinstance(summary, dict) else None,
        summary.get("pending_blank_count") if isinstance(summary, dict) else None,
        0,
    )
    completed_decision_count = _first_int(
        payload.get("completed_decision_count") if isinstance(payload, dict) else None,
        summary.get("completed_decision_count") if isinstance(summary, dict) else None,
        _derived_completed_decision_count(payload),
        0,
    )
    invalid_count = _first_int(
        payload.get("invalid_count") if isinstance(payload, dict) else None,
        summary.get("invalid_count") if isinstance(summary, dict) else None,
        0,
    )
    import_ready_present = isinstance(summary, dict) and "import_ready_count" in summary
    import_ready_count = _first_int(summary.get("import_ready_count"), 0) if import_ready_present else (
        completed_decision_count if invalid_count == 0 else 0
    )
    return {
        "row_count": row_count,
        "pending_blank_count": pending_blank_count,
        "completed_decision_count": completed_decision_count,
        "invalid_count": invalid_count,
        "import_ready_count": import_ready_count,
        "sensitive_reference_count": _first_int(
            payload.get("sensitive_reference_count") if isinstance(payload, dict) else None,
            summary.get("sensitive_reference_count") if isinstance(summary, dict) else None,
            0,
        ),
        "guardrail_issue_count": _first_int(
            payload.get("guardrail_issue_count") if isinstance(payload, dict) else None,
            summary.get("guardrail_issue_count") if isinstance(summary, dict) else None,
            0,
        ),
    }


def _recommended_next_actions(
    summaries: dict[str, Any],
    source_issue_counts: dict[str, int],
    payloads: dict[str, Any | None],
) -> list[str]:
    actions: list[str] = []
    priority_counts = ((summaries.get("priority") or {}).get("priority_counts") or {})
    decision = summaries.get("decision_audit") or {}
    claim_queue = summaries.get("claim_queue") or {}

    if priority_counts.get("P0", 0):
        actions.append("Review P0 SQF claims first with the linked Human Review packet.")
    if priority_counts.get("reject_review", 0):
        actions.append("Keep reject_review items out of any guarded import candidate set.")
    if decision.get("pending_blank_count", 0):
        actions.append("Collect explicit human approve/reject/defer decisions with rationale and packet evidence.")
    if decision.get("completed_decision_count", 0) and not decision.get("invalid_count", 0):
        actions.append("Rerun the decision audit after recording decisions before any guarded import planning.")
    if source_issue_counts.get("source_guardrail_issue_count") or source_issue_counts.get("sensitive_reference_count"):
        actions.append("Regenerate source artifacts after fixing guardrail and public-report hygiene issues.")
    if decision.get("invalid_count", 0):
        actions.append("Fix invalid decision rows before treating any completed decisions as import-ready.")
    if claim_queue.get("claim_count", 0) and not priority_counts:
        actions.append("Run SQF review-priority on claim candidates before sequencing human review.")
    if payloads.get("claim_report") is None:
        actions.append("Generate SQF claim candidates before readiness review.")
    if payloads.get("decision_audit") is None:
        actions.append("Run the decision audit after the decision sheet is prepared.")
    if not actions:
        actions.append("Continue Human Review sequencing; keep SQF evidence supplemental and rerun readiness after decisions.")
    return actions


def _top_level_ok(payload: Any) -> bool | None:
    if isinstance(payload, dict) and "ok" in payload:
        return _as_bool(payload.get("ok"))
    return None


def _input_guardrail_issue_count(payload: Any) -> int:
    count = 0
    for key, value in _walk_items(payload):
        if key in REPORT_ONLY_GUARDRAILS and _value_is_trueish(value):
            count += 1
    return count


def _explicit_guardrail_issue_count(source_key: str, payload: Any) -> int:
    if source_key == "priority_report" and isinstance(payload, dict):
        return _first_int(_nested_get(payload, ("summary", "source_guardrail_issue_count")), 0)
    if source_key == "decision_audit" and isinstance(payload, dict):
        return _first_int(payload.get("guardrail_issue_count"), _nested_get(payload, ("summary", "guardrail_issue_count")), 0)
    return 0


def _explicit_sensitive_count(source_key: str, payload: Any) -> int:
    if source_key == "decision_audit" and isinstance(payload, dict):
        return _first_int(
            payload.get("sensitive_reference_count"),
            _nested_get(payload, ("summary", "sensitive_reference_count")),
            0,
        )
    return 0


def _explicit_invalid_count(source_key: str, payload: Any) -> int:
    if source_key == "decision_audit" and isinstance(payload, dict):
        return _first_int(_nested_get(payload, ("summary", "invalid_count")), payload.get("invalid_count"), 0)
    if isinstance(payload, dict):
        return _first_int(payload.get("invalid_count"), _nested_get(payload, ("summary", "invalid_count")), 0)
    return 0


def _sensitive_reference_count(payload: Any) -> int:
    count = 0
    for key, value in _walk_items(payload):
        if _contains_sensitive_marker(key):
            count += 1
        elif isinstance(value, str) and (_contains_sensitive_marker(value) or _looks_like_internal_path(value)):
            count += 1
    return count


def _walk_items(value: Any, key: str = "") -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            child_key_text = str(child_key)
            items.append((child_key_text, child_value))
            items.extend(_walk_items(child_value, child_key_text))
    elif isinstance(value, list):
        for child in value:
            items.extend(_walk_items(child, key))
    return items


def _contains_sensitive_marker(value: Any) -> bool:
    text = str(value or "").casefold()
    return any(marker in text for marker in SENSITIVE_MARKERS)


def _looks_like_internal_path(value: str) -> bool:
    text = value.strip()
    if not text or "://" in text:
        return False
    if PureWindowsPath(text).is_absolute() or PurePosixPath(text).is_absolute():
        return True
    return False


def _safe_artifact_name(path: Path) -> str:
    name = path.name
    if _contains_sensitive_marker(name):
        return "redacted_artifact"
    return name


def _append_finding(
    findings: list[dict[str, Any]],
    *,
    source: str,
    severity: str,
    code: str,
    message: str,
    count: int | None = None,
    artifact: str | None = None,
) -> None:
    finding: dict[str, Any] = {
        "severity": severity,
        "source": source,
        "code": code,
        "message": message,
    }
    if count is not None:
        finding["count"] = int(count)
    if artifact:
        finding["artifact"] = artifact
    findings.append(finding)


def _claim_records(payload: Any | None) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]
    if isinstance(payload, dict):
        claims = payload.get("claims")
        if isinstance(claims, list):
            return [record for record in claims if isinstance(record, dict)]
        items = payload.get("items")
        if isinstance(items, list):
            return [record for record in items if isinstance(record, dict)]
    return []


def _count_claim_jobs(claims: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for claim in claims:
        sqf = claim.get("sqf") or {}
        scope = claim.get("scope") or {}
        job = sqf.get("job_name") or scope.get("sqf_job_name") or claim.get("job_name")
        if job:
            counter[str(job)] += 1
    return dict(counter)


def _count_claim_sources(claims: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for claim in claims:
        source = claim.get("source_type") or claim.get("source") or (claim.get("sqf_ncs_match") or {}).get("source_type")
        if source:
            counter[str(source)] += 1
    return dict(counter)


def _count_claim_relations(claims: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for claim in claims:
        basis = claim.get("basis_strength") or {}
        match = claim.get("sqf_ncs_match") or {}
        relation = basis.get("mapping_relation") or match.get("relation") or claim.get("mapping_relation")
        if relation:
            counter[str(relation)] += 1
    return dict(counter)


def _derived_completed_decision_count(payload: Any | None) -> int:
    if not isinstance(payload, dict):
        return 0
    count = 0
    for row in payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        decision = str(row.get("decision") or "").strip().lower()
        if decision in {"approve", "reject", "defer"}:
            count += 1
    return count


def _nested_get(payload: Any, keys: tuple[str, ...]) -> Any:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_int(*values: Any) -> int:
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            return int(value)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _list_len(value: Any) -> int | None:
    if isinstance(value, list):
        return len(value)
    return None


def _first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _int_count_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _first_int(raw_value, 0) for key, raw_value in sorted(value.items(), key=lambda item: str(item[0]))}


def _compact_counts(value: dict[str, Any]) -> dict[str, Any]:
    return {key: count for key, count in value.items() if count not in (None, 0, {}, [])}


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return None


def _value_is_trueish(value: Any) -> bool:
    return _as_bool(value) is True


def _json_inline(value: Any) -> str:
    return _markdown_text(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _markdown_text(value: Any) -> str:
    text = "" if value is None else str(value)
    sanitized = text
    for marker in SENSITIVE_MARKERS:
        sanitized = sanitized.replace(marker, "[redacted]")
    return sanitized.replace("|", "\\|").replace("\n", " ")
