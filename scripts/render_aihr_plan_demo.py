"""Render AI-HR education-plan JSON artifacts into a compact HTML dashboard."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any


REQUIRED_DELIVERY_FIT_KEYS = {"level", "hours", "methods", "facilities"}
REQUIRED_GUIDE_TRACE_CODES = {
    "job_scope",
    "task_ksa",
    "course_link",
    "required_optional",
    "level_delivery",
    "human_review",
}
REQUIRED_GUIDE_WORKFLOW_STAGE_CODES = {"C1-1", "C1-2", "C2-1", "C2-2"}
REQUIRED_RECOMMENDED_PATH_GUIDE_STAGES = {
    "scope_confirmation": "C1-1",
    "core_gap_training": "C1-2",
    "supporting_or_adjacent_training": "C2-1",
    "delivery_fit_review": "C2-2",
}
SENSITIVE_DEMO_MARKERS = (
    "source_payload",
    "source_rows",
    "source_json",
    "raw_payload",
    "raw_payloads",
    "raw_response",
    "review_status",
    "authKey",
    "serviceKey",
    "service_key",
    "NCS_SERVICE_KEY",
    "NCS_TRAINING_COURSE_SERVICE_KEY",
    "NCS_QUALIFICATION_SERVICE_KEY",
    "NCS_JOB_BASE_SERVICE_KEY",
)
SENSITIVE_DEMO_MARKERS_LOWER = tuple(marker.lower() for marker in SENSITIVE_DEMO_MARKERS)
PUBLIC_DEMO_REDACTED_VALUE = "[REDACTED]"
PUBLIC_DEMO_VALUE_MARKERS_LOWER = (
    *SENSITIVE_DEMO_MARKERS_LOWER,
    "human_reviewed",
    "accepted",
    "reviewed",
)
PUBLIC_REVIEW_CONTEXT_POLICY = {
    "schema": "aihr_public_review_context_policy_v1",
    "review_only": True,
    "non_scoring": True,
    "approval_claim": False,
    "db_writes": False,
    "status_update_allowed": False,
    "source_payload_exposed": False,
    "official_learning_module_rule": (
        "Official learning-module evidence requires explicit official EDU links; "
        "OCR and NCS-report context do not create official module evidence."
    ),
    "ocr_report_context_rule": (
        "Learning-module OCR, NCS report/reference, REPORT-TRAINING, and NCS-DERIVED "
        "context are Human Review evidence only and must not affect scoring, DB writes, "
        "or status updates."
    ),
}
REVIEW_CONTEXT_IMPLICATION_MARKERS = (
    "human_reviewed",
    "accepted",
    "reviewed",
    "approved",
    "official approval",
    "approval granted",
    "write db",
    "db write",
    "db_writes=true",
    "status_update_allowed=true",
    "status update allowed",
    "raise recommendation score",
    "recommendation scoring",
    "score uplift",
    "scoring uplift",
    "non_scoring=false",
)
REVIEW_CONTEXT_IMPLICATION_MARKERS_LOWER = tuple(
    marker.lower() for marker in REVIEW_CONTEXT_IMPLICATION_MARKERS
)
REQUIRED_REVIEW_CONTEXT_GUARDRAILS = {
    "review_only": True,
    "non_scoring": True,
    "approval_claim": False,
    "db_writes": False,
    "status_update_allowed": False,
}
PUBLIC_DEMO_STRIP_KEYS = {
    "authKey",
    "relation_id",
    "created_at",
    "updated_at",
    "review_status",
    "data_sources",
    "serviceKey",
    "service_key",
    "NCS_SERVICE_KEY",
    "NCS_TRAINING_COURSE_SERVICE_KEY",
    "NCS_QUALIFICATION_SERVICE_KEY",
    "NCS_JOB_BASE_SERVICE_KEY",
    "source_payload",
    "source_rows",
    "source_json",
    "source_url_or_document",
    "raw_payload",
    "raw_payloads",
    "raw_response",
    "api_payload",
    "api_response",
}
PUBLIC_DEMO_STRIP_KEYS_LOWER = {key.lower() for key in PUBLIC_DEMO_STRIP_KEYS}


def _public_demo_text(value: Any) -> str:
    text = "" if value is None else str(value)
    lower_text = text.lower()
    if any(marker in lower_text for marker in PUBLIC_DEMO_VALUE_MARKERS_LOWER):
        return PUBLIC_DEMO_REDACTED_VALUE
    return text


def _review_context_text(value: Any) -> str:
    text = _public_demo_text(value)
    if text == PUBLIC_DEMO_REDACTED_VALUE:
        return text
    lower_text = text.lower()
    if any(marker in lower_text for marker in REVIEW_CONTEXT_IMPLICATION_MARKERS_LOWER):
        return PUBLIC_DEMO_REDACTED_VALUE
    return text


def public_demo_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a public-facing copy without internal audit metadata."""

    def should_strip_key(key: Any) -> bool:
        key_text = str(key)
        key_lower = key_text.lower()
        return (
            key_text in PUBLIC_DEMO_STRIP_KEYS
            or key_lower in PUBLIC_DEMO_STRIP_KEYS_LOWER
            or (key_lower.startswith("ncs_") and key_lower.endswith("_service_key"))
            or "review_status" in key_lower
            or "review_state_counts" in key_lower
            or "human_reviewed" in key_lower
            or "trusted_reviewed" in key_lower
            or "reviewed_goal" in key_lower
            or key_lower in {"reviewed", "accepted"}
        )

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: redact(item)
                for key, item in value.items()
                if not should_strip_key(key)
            }
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            return _public_demo_text(value)
        return value

    redacted = redact(payload)
    if isinstance(redacted, dict):
        redacted["public_demo_schema"] = "aihr_public_demo_v1"
        redacted["public_demo_notice"] = (
            "시제품 검증 화면이며 공식 승인, 자격 인정, 법적 적격성 판단 화면이 아닙니다."
        )
        redacted["review_context_policy"] = dict(PUBLIC_REVIEW_CONTEXT_POLICY)
    return redacted


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _join(values: Any) -> str:
    if not values:
        return "-"
    if isinstance(values, list):
        return ", ".join(_esc(value) for value in values if value)
    return _esc(values)


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return [item for item in value if item not in (None, "")]
    if value in (None, ""):
        return []
    return [value]


def _unit_base_code(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.split("_", 1)[0]


def _payload_scope_base_codes(payload: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    for scope_key in ("current_scope", "target_scope"):
        scope = payload.get(scope_key) if isinstance(payload.get(scope_key), dict) else {}
        alias = scope.get("query_alias") if isinstance(scope.get("query_alias"), dict) else {}
        for candidate in (alias.get("unit_code"), scope.get("unit_code")):
            base = _unit_base_code(candidate)
            if base:
                codes.add(base)
    interpretation = (
        payload.get("scope_interpretation")
        if isinstance(payload.get("scope_interpretation"), dict)
        else {}
    )
    for scope_key in ("current", "target"):
        scope = interpretation.get(scope_key) if isinstance(interpretation.get(scope_key), dict) else {}
        for candidate in scope.get("unit_codes") or []:
            base = _unit_base_code(candidate)
            if base:
                codes.add(base)
    return codes


def _render_bullets(values: Any, *, limit: int = 5) -> str:
    items = _as_list(values)
    if not items:
        return '<span class="muted">-</span>'
    shown = items[:limit]
    remainder = len(items) - len(shown)
    body = "".join(f"<li>{_esc(item)}</li>" for item in shown)
    if remainder > 0:
        body += f'<li class="muted">+{remainder} more</li>'
    return f'<ul class="mini-list">{body}</ul>'


def _render_dict_brief(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return '<span class="muted">-</span>'
    parts = []
    for key, item in value.items():
        if item in (None, "", []):
            continue
        parts.append(f'<span><strong>{_esc(key)}</strong>: {_join(item)}</span>')
    if not parts:
        return '<span class="muted">-</span>'
    return '<div class="kv-list">' + "".join(parts) + "</div>"


def _render_review_context_flag(label: str, value: Any) -> str:
    if value is True:
        return f"{_esc(label)}=True"
    if value is False:
        return f"{_esc(label)}=False"
    if value in (None, ""):
        return f"{_esc(label)}=-"
    return f"{_esc(label)}={_esc(value)}"


def _review_context_guardrail_issues(item: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for key, expected in REQUIRED_REVIEW_CONTEXT_GUARDRAILS.items():
        actual = item.get(key)
        if actual is expected:
            continue
        if key not in item or actual in (None, ""):
            issues.append(f"{key}=missing_expected_{expected}")
        else:
            issues.append(f"{key}=expected_{expected}_got_{_public_demo_text(actual)}")
    return issues


def _review_context_guardrails(item: dict[str, Any]) -> list[str]:
    issues = _review_context_guardrail_issues(item)
    if issues:
        return ["guardrail_contract_blocked", *issues]
    return [
        _render_review_context_flag("review_only", item.get("review_only")),
        _render_review_context_flag("non_scoring", item.get("non_scoring")),
        _render_review_context_flag("approval_claim", item.get("approval_claim")),
        _render_review_context_flag("db_writes", item.get("db_writes")),
        _render_review_context_flag("status_update_allowed", item.get("status_update_allowed")),
    ]


def _blocked_review_context_row(
    source_path: Path,
    schema: str,
    unit: Any,
    issues: list[str],
) -> dict[str, Any]:
    return {
        "source": _public_demo_text(source_path.name),
        "schema": _public_demo_text(schema),
        "unit": _public_demo_text(unit),
        "label": "context hidden until guardrail contract is complete",
        "official": "blocked",
        "ocr": "blocked",
        "report": "blocked",
        "gap": "guardrail_contract_blocked",
        "guardrails": ["guardrail_contract_blocked", *issues],
    }


def _review_context_top_terms(value: Any, *, limit: int = 3) -> list[str]:
    if not isinstance(value, list):
        return []
    terms: list[str] = []
    for item in value:
        if isinstance(item, dict):
            term = item.get("term")
        else:
            term = item
        if term in (None, ""):
            continue
        terms.append(str(term))
        if len(terms) >= limit:
            break
    return terms


def _review_context_snippet_count(context: dict[str, Any]) -> int | None:
    signal = context.get("snippet_signal")
    if isinstance(signal, dict):
        found_count = signal.get("found_count")
        if isinstance(found_count, int):
            return found_count
        try:
            return int(found_count)
        except (TypeError, ValueError):
            pass
    snippets = context.get("snippets")
    if isinstance(snippets, dict):
        count = 0
        for snippet in snippets.values():
            if isinstance(snippet, dict) and snippet.get("found") is True:
                count += 1
        return count
    return None


def _review_context_page_count(context: dict[str, Any]) -> Any:
    return context.get("page_count") or context.get("pages") or "-"


def _review_context_ocr_summary(
    context: dict[str, Any] | None,
    *,
    fallback_status: str = "-",
    fallback_pages: Any = "-",
) -> str:
    if not isinstance(context, dict):
        return _review_context_text(f"{fallback_status}; pages={fallback_pages}")
    status = context.get("status") or fallback_status
    page_count = _review_context_page_count(context)
    if page_count in (None, ""):
        page_count = fallback_pages
    parts = [f"{status}; pages={page_count}"]
    snippet_count = _review_context_snippet_count(context)
    if snippet_count is not None:
        parts.append(f"snippet_hits={snippet_count}")
    top_terms = _review_context_top_terms(context.get("top_terms"))
    if top_terms:
        parts.append("top_terms=" + ", ".join(top_terms))
    return _review_context_text("; ".join(parts))


def _review_context_report_summary(unit: dict[str, Any], report_rows: Any) -> str:
    parts: list[str] = []
    if report_rows not in (None, ""):
        parts.append(f"NCS report rows: {report_rows}")
    reference_chunks = unit.get("reference_chunk_count")
    if reference_chunks not in (None, "", report_rows):
        parts.append(f"reference chunks: {reference_chunks}")
    report_training_courses = unit.get("report_training_course_count")
    if report_training_courses not in (None, ""):
        parts.append(f"report courses: {report_training_courses}")
    if not parts:
        parts.append("-")
    return _review_context_text("; ".join(parts))


def _review_context_rows(
    payload: dict[str, Any],
    contexts: list[tuple[Path, dict[str, Any]]],
) -> list[dict[str, Any]]:
    scope_bases = _payload_scope_base_codes(payload)
    rows: list[dict[str, Any]] = []
    for source_path, context in contexts:
        schema = str(context.get("schema") or "")
        if schema == "aihr_hr_learning_module_ocr_context_cards_v2":
            for card in context.get("cards") or []:
                if not isinstance(card, dict):
                    continue
                base_code = _unit_base_code(card.get("unit_base_code"))
                if scope_bases and base_code not in scope_bases:
                    continue
                guardrail_issues = _review_context_guardrail_issues(card)
                if guardrail_issues:
                    rows.append(
                        _blocked_review_context_row(
                            source_path,
                            schema,
                            base_code,
                            guardrail_issues,
                        )
                    )
                    continue
                rows.append(
                    {
                        "source": _public_demo_text(source_path.name),
                        "schema": _public_demo_text(schema),
                        "unit": _public_demo_text(base_code),
                        "label": _review_context_text(card.get("module_name") or base_code),
                        "official": "-",
                        "ocr": _review_context_ocr_summary(
                            card,
                            fallback_status=card.get("status") or "-",
                            fallback_pages=card.get("page_count") or "-",
                        ),
                        "report": "-",
                        "gap": "OCR learning-module context only",
                        "guardrails": _review_context_guardrails(card),
                    }
                )
            continue

        units = context.get("units") if isinstance(context.get("units"), list) else []
        for unit in units:
            if not isinstance(unit, dict):
                continue
            code = unit.get("ncs_cl_cd") or unit.get("unit_code") or unit.get("base_unit_code")
            base_code = _unit_base_code(code)
            if scope_bases and base_code not in scope_bases:
                continue
            guardrail_issues = _review_context_guardrail_issues(unit)
            if guardrail_issues:
                rows.append(
                    _blocked_review_context_row(
                        source_path,
                        schema,
                        code or base_code,
                        guardrail_issues,
                    )
                )
                continue
            report_rows = unit.get("ncs_report_visible_rows")
            if report_rows is None:
                report_rows = unit.get("reference_chunk_count")
            gap_reasons = unit.get("learning_module_gap_reason_counts")
            if isinstance(gap_reasons, dict) and gap_reasons:
                gap_text = ", ".join(str(key) for key in gap_reasons)
            else:
                gap_text = unit.get("gap_reason") or unit.get("recommended_review_action") or "-"
            unit_ocr_context = (
                unit.get("learning_module_ocr_context")
                if isinstance(unit.get("learning_module_ocr_context"), dict)
                else None
            )
            rows.append(
                {
                    "source": _public_demo_text(source_path.name),
                    "schema": _public_demo_text(schema),
                    "unit": _public_demo_text(code or base_code),
                    "label": _review_context_text(
                        unit.get("unit_name") or unit.get("target_label") or base_code
                    ),
                    "official": _review_context_text(
                        f"official direct links: {unit.get('official_direct_link_count')}"
                        if "official_direct_link_count" in unit
                        else f"official modules: {unit.get('official_learning_module_count')}"
                        if "official_learning_module_count" in unit
                        else "-"
                    ),
                    "ocr": _review_context_text(
                        _review_context_ocr_summary(
                            unit_ocr_context,
                            fallback_status="OCR: available",
                            fallback_pages=unit_ocr_context.get("page_count", "-")
                            if isinstance(unit_ocr_context, dict)
                            else "-",
                        )
                        if unit.get("learning_module_ocr_context_available") is True
                        else "OCR: not shown"
                        if "learning_module_ocr_context_available" in unit
                        else "-"
                    ),
                    "report": _review_context_report_summary(unit, report_rows),
                    "gap": _review_context_text(gap_text),
                    "guardrails": _review_context_guardrails(unit),
                }
            )
    return rows


def _render_review_context_annex(
    payload: dict[str, Any],
    contexts: list[tuple[Path, dict[str, Any]]],
) -> str:
    if not contexts:
        return ""
    rows = _review_context_rows(payload, contexts)
    if not rows:
        return (
            '<div class="review-context">'
            "<h3>Human Review Context Annex / 학습모듈·NCS 보고서 참고근거</h3>"
            '<p class="muted">No review-context rows matched the current or target NCS scope.</p>'
            "</div>"
        )
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{_esc(row.get('source'))}<br><span class=\"muted\">{_esc(row.get('schema'))}</span></td>"
            f"<td>{_esc(row.get('unit'))}<br><strong>{_esc(row.get('label'))}</strong></td>"
            f"<td>{_esc(row.get('official'))}</td>"
            f"<td>{_esc(row.get('ocr'))}</td>"
            f"<td>{_esc(row.get('report'))}</td>"
            f"<td>{_esc(row.get('gap'))}</td>"
            f"<td>{_render_bullets(row.get('guardrails'), limit=5)}</td>"
            "</tr>"
        )
    return (
        '<div class="review-context">'
        "<h3>Human Review Context Annex / 학습모듈·NCS 보고서 참고근거</h3>"
        '<p class="prototype-notice">OCR, REPORT-TRAINING, NCS-DERIVED, and reference-document evidence are Human Review context only. They must not raise recommendation scores, write DB status, or imply approval.</p>'
        '<table class="contract"><thead><tr>'
        "<th>Source</th><th>Unit</th><th>Official EDU</th><th>OCR</th><th>NCS Report</th><th>Gap / Action</th><th>Guardrails</th>"
        "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )


def _field(label: str, value: Any, *, limit: int = 5) -> str:
    return (
        '<div class="cell-block">'
        f'<span class="subtle-label">{_esc(label)}</span>'
        f"{_render_bullets(value, limit=limit)}"
        "</div>"
    )


def _status_label(value: Any, *, pass_values: set[str] | None = None) -> str:
    label = str(value or "unknown")
    ok_values = pass_values or {"ready", "fit", "clear", "pass", "matched", "complete"}
    neutral_values = {"not_requested", "unknown", "not_applicable"}
    if label in ok_values:
        klass = "pass"
    elif label in neutral_values:
        klass = "neutral"
    else:
        klass = "warn"
    return f'<span class="chip {klass}">{_esc(label)}</span>'


def _row_facility_fit_status(row: dict[str, Any]) -> str:
    fit = row.get("facility_constraint_fit") if isinstance(row.get("facility_constraint_fit"), dict) else {}
    if not fit:
        delivery = row.get("delivery_operation") if isinstance(row.get("delivery_operation"), dict) else {}
        fit = delivery.get("facility_constraint_fit") if isinstance(delivery.get("facility_constraint_fit"), dict) else {}
    return str(fit.get("status") or "")


def _missing_delivery_fit_fields(row: dict[str, Any]) -> list[str]:
    fit = row.get("course_fit") if isinstance(row.get("course_fit"), dict) else {}
    facility_status = _row_facility_fit_status(row)
    allows_sparse_delivery = facility_status in {"unknown", "not_requested"}
    missing: list[str] = []
    for field in REQUIRED_DELIVERY_FIT_KEYS:
        if field not in fit:
            missing.append(field)
            continue
        value = fit.get(field)
        if field in {"methods", "facilities"}:
            if not isinstance(value, list):
                missing.append(field)
            elif not [item for item in value if str(item).strip()] and not allows_sparse_delivery:
                missing.append(field)
        elif value is None or value == "":
            missing.append(field)
    return sorted(missing)


def _missing_matrix_contract_fields(rows: list[Any]) -> list[str]:
    required_row_dict_fields = {
        "job_scope",
        "target_level_band",
        "education_type",
        "required_optional_basis",
        "delivery_operation",
        "planner_grouping",
        "task_ksa_basis",
        "course_link",
        "course_scope_fit",
        "facility_constraint_fit",
        "specificity_warning",
        "duplicate_or_generic_warning",
        "mapping_strength",
        "mapping_strength_warning",
        "decision_state",
        "evidence_chain",
        "human_review",
    }
    required_row_value_fields = {"required_optional"}
    required_task_ksa_fields = {
        "basis_types",
        "target_scope_ksa",
        "gap_ksa",
        "training_goal_ksa",
        "covered_elements",
    }
    required_facility_fit_fields = {
        "status",
        "requested",
        "available",
        "matched",
        "missing",
        "rationale",
    }
    required_human_review_fields = {"severity", "prompt", "action", "review_board_hint", "flags"}
    required_decision_state_fields = {
        "schema",
        "status",
        "decision_required",
        "system_suggestion",
        "allowed_decisions",
        "approval_claim",
        "message",
    }
    required_evidence_chain_fields = {"schema", "chain_order", "links", "completeness", "message"}
    required_course_link_fields = {
        "course_name",
        "training_course_id",
        "mapping_chain",
        "evidence_directness",
        "need_classification",
        "basis_types",
        "course_scope_fit",
        "why_recommended",
    }
    missing: list[str] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            missing.append(f"row_{index}:not_object")
            continue
        for field in sorted(required_row_dict_fields):
            if not isinstance(row.get(field), dict):
                missing.append(f"row_{index}:{field}")
        for field in sorted(required_row_value_fields):
            if row.get(field) in (None, ""):
                missing.append(f"row_{index}:{field}")
        task_ksa_basis = row.get("task_ksa_basis")
        if isinstance(task_ksa_basis, dict):
            for field in sorted(required_task_ksa_fields - set(task_ksa_basis)):
                missing.append(f"row_{index}:task_ksa_basis.{field}")
        course_link = row.get("course_link")
        if isinstance(course_link, dict):
            for field in sorted(required_course_link_fields - set(course_link)):
                missing.append(f"row_{index}:course_link.{field}")
        facility_fit = row.get("facility_constraint_fit")
        if isinstance(facility_fit, dict):
            for field in sorted(required_facility_fit_fields - set(facility_fit)):
                missing.append(f"row_{index}:facility_constraint_fit.{field}")
        human_review = row.get("human_review")
        if isinstance(human_review, dict):
            for field in sorted(required_human_review_fields - set(human_review)):
                missing.append(f"row_{index}:human_review.{field}")
        decision_state = row.get("decision_state")
        if isinstance(decision_state, dict):
            for field in sorted(required_decision_state_fields - set(decision_state)):
                missing.append(f"row_{index}:decision_state.{field}")
            if decision_state.get("schema") != "aihr_training_row_decision_state_v1":
                missing.append(f"row_{index}:decision_state.schema:{decision_state.get('schema')}")
            if decision_state.get("status") != "pending_human_decision":
                missing.append(f"row_{index}:decision_state.status:{decision_state.get('status')}")
            if decision_state.get("approval_claim") is not False:
                missing.append(f"row_{index}:decision_state.approval_claim")
        evidence_chain = row.get("evidence_chain")
        if isinstance(evidence_chain, dict):
            for field in sorted(required_evidence_chain_fields - set(evidence_chain)):
                missing.append(f"row_{index}:evidence_chain.{field}")
            if evidence_chain.get("schema") != "aihr_course_evidence_chain_v1":
                missing.append(f"row_{index}:evidence_chain.schema:{evidence_chain.get('schema')}")
            if not isinstance(evidence_chain.get("links"), list) or not evidence_chain.get("links"):
                missing.append(f"row_{index}:evidence_chain.links")
    return missing


def _missing_recommended_path_fields(payload: dict[str, Any]) -> list[str]:
    path = payload.get("recommended_path")
    if not isinstance(path, list) or not path:
        return ["recommended_path"]
    missing: list[str] = []
    roles = set()
    guide_codes = set()
    for index, stage in enumerate(path, start=1):
        if not isinstance(stage, dict):
            missing.append(f"recommended_path.row_{index}")
            continue
        role = str(stage.get("role") or "")
        roles.add(role)
        guide_stage = str(stage.get("guide_stage") or "")
        if guide_stage:
            guide_codes.add(guide_stage)
        for field in ("stage", "role", "title", "guide_stage", "guide_stage_status"):
            if not stage.get(field):
                missing.append(f"recommended_path.row_{index}.{field}")
        if stage.get("guide_stage_status") not in {"ready", "needs_review"}:
            missing.append(
                f"recommended_path.row_{index}.guide_stage_status:{stage.get('guide_stage_status')}"
            )
        if not isinstance(stage.get("guide_stage_evidence"), dict):
            missing.append(f"recommended_path.row_{index}.guide_stage_evidence")
        expected_guide_stage = REQUIRED_RECOMMENDED_PATH_GUIDE_STAGES.get(role)
        if expected_guide_stage and guide_stage != expected_guide_stage:
            missing.append(f"recommended_path.role.{role}.guide_stage:{guide_stage}")
    required_roles = set(REQUIRED_RECOMMENDED_PATH_GUIDE_STAGES)
    for role in sorted(required_roles - roles):
        missing.append(f"recommended_path.role.{role}")
    for code in sorted(set(REQUIRED_RECOMMENDED_PATH_GUIDE_STAGES.values()) - guide_codes):
        missing.append(f"recommended_path.guide_stage.{code}")
    return missing


def _missing_scope_baseline_fields(payload: dict[str, Any]) -> list[str]:
    baseline = payload.get("scope_baseline")
    if not isinstance(baseline, dict):
        return ["scope_baseline"]
    missing: list[str] = []
    if baseline.get("schema") != "aihr_scope_baseline_v1":
        missing.append(f"scope_baseline.schema:{baseline.get('schema')}")
    for field in (
        "guide_stage",
        "purpose",
        "ncs_scope_relation",
        "current_scope_subset_of_target",
        "exact_ksa_overlap_ratio",
        "ontology_adjusted_transferability_ratio",
        "adjusted_transferability_components",
        "human_review",
    ):
        if field not in baseline:
            missing.append(f"scope_baseline.{field}")
    for role in ("current", "target"):
        entry = baseline.get(role)
        if not isinstance(entry, dict):
            missing.append(f"scope_baseline.{role}")
            continue
        for field in (
            "requested_query",
            "resolved_scope",
            "match_level",
            "unit_count",
            "scope_resolution_basis",
        ):
            if field not in entry:
                missing.append(f"scope_baseline.{role}.{field}")
    human_review = baseline.get("human_review")
    if isinstance(human_review, dict):
        for field in ("status", "flags", "prompt"):
            if field not in human_review:
                missing.append(f"scope_baseline.human_review.{field}")
    return missing


def _missing_course_intake_requirements_fields(payload: dict[str, Any]) -> list[str]:
    intake = payload.get("course_intake_requirements")
    if not isinstance(intake, dict):
        return ["course_intake_requirements"]
    missing: list[str] = []
    if intake.get("schema") != "aihr_course_intake_requirements_v1":
        missing.append(f"course_intake_requirements.schema:{intake.get('schema')}")
    for field in (
        "guide_stage",
        "status",
        "purpose",
        "target_population",
        "requested_constraints",
        "required_fields",
        "optional_fields",
        "mapping_policy",
        "prefill_from_recommendations",
        "review_gate",
    ):
        if field not in intake:
            missing.append(f"course_intake_requirements.{field}")
    required_fields = intake.get("required_fields")
    if not isinstance(required_fields, list) or not required_fields:
        missing.append("course_intake_requirements.required_fields")
    else:
        required_names = {
            "course_name",
            "course_goal",
            "target_learners",
            "content_outline",
            "ncs_scope_or_unit",
            "performance_criteria_or_task",
            "ksa_evidence",
            "level",
            "hours",
            "methods",
            "facilities",
            "assessment_method",
        }
        names = {
            str(item.get("field"))
            for item in required_fields
            if isinstance(item, dict) and item.get("field")
        }
        for field in sorted(required_names - names):
            missing.append(f"course_intake_requirements.required_fields.{field}")
        for index, item in enumerate(required_fields, start=1):
            if not isinstance(item, dict):
                missing.append(f"course_intake_requirements.required_fields.row_{index}")
                continue
            for field in ("field", "purpose", "maps_to"):
                if field not in item:
                    missing.append(f"course_intake_requirements.required_fields.row_{index}.{field}")
    mapping_policy = intake.get("mapping_policy") if isinstance(intake.get("mapping_policy"), dict) else {}
    for field in (
        "title_only_mapping_allowed",
        "n_to_n_job_course_mapping_allowed",
        "generic_course_requires_warning",
        "framework_reference_is_not_scoring_source",
        "human_review_required_before_approval",
    ):
        if field not in mapping_policy:
            missing.append(f"course_intake_requirements.mapping_policy.{field}")
    if mapping_policy.get("title_only_mapping_allowed") is not False:
        missing.append("course_intake_requirements.mapping_policy.title_only_mapping_allowed")
    if mapping_policy.get("framework_reference_is_not_scoring_source") is not True:
        missing.append("course_intake_requirements.mapping_policy.framework_reference_is_not_scoring_source")
    if mapping_policy.get("human_review_required_before_approval") is not True:
        missing.append("course_intake_requirements.mapping_policy.human_review_required_before_approval")
    review_gate = intake.get("review_gate") if isinstance(intake.get("review_gate"), dict) else {}
    if not review_gate:
        missing.append("course_intake_requirements.review_gate")
    elif review_gate.get("approval_claim") is not False:
        missing.append("course_intake_requirements.review_gate.approval_claim")
    return missing


def _missing_training_course_inventory_template_fields(payload: dict[str, Any]) -> list[str]:
    template = payload.get("training_course_inventory_template")
    if not isinstance(template, dict):
        return ["training_course_inventory_template"]
    missing: list[str] = []
    if template.get("schema") != "aihr_training_course_inventory_template_v1":
        missing.append(f"training_course_inventory_template.schema:{template.get('schema')}")
    for field in (
        "guide_stage",
        "status",
        "purpose",
        "target_population",
        "requested_constraints",
        "columns",
        "required_columns",
        "row_template",
        "prefill_rows",
        "validation_rules",
        "review_gate",
    ):
        if field not in template:
            missing.append(f"training_course_inventory_template.{field}")
    required_columns = template.get("required_columns")
    columns = template.get("columns")
    if not isinstance(required_columns, list) or not required_columns:
        missing.append("training_course_inventory_template.required_columns")
    if not isinstance(columns, list) or not columns:
        missing.append("training_course_inventory_template.columns")
    else:
        expected_required = {
            "source_type",
            "course_name",
            "course_goal",
            "target_learners",
            "content_outline",
            "ncs_scope_or_unit",
            "performance_criteria_or_task",
            "ksa_evidence",
            "level",
            "hours",
            "methods",
            "facilities",
            "education_type",
            "required_optional_basis",
            "assessment_method",
            "duplicate_or_generic_risk",
            "review_state",
        }
        column_names = {
            str(item.get("column"))
            for item in columns
            if isinstance(item, dict) and item.get("column")
        }
        for field in sorted(expected_required - column_names):
            missing.append(f"training_course_inventory_template.columns.{field}")
        if isinstance(required_columns, list):
            for field in sorted(expected_required - {str(item) for item in required_columns}):
                missing.append(f"training_course_inventory_template.required_columns.{field}")
        for index, item in enumerate(columns, start=1):
            if not isinstance(item, dict):
                missing.append(f"training_course_inventory_template.columns.row_{index}")
                continue
            for field in ("column", "required", "purpose", "maps_to", "validation"):
                if field not in item:
                    missing.append(f"training_course_inventory_template.columns.row_{index}.{field}")
    row_template = template.get("row_template")
    if not isinstance(row_template, dict):
        missing.append("training_course_inventory_template.row_template")
    elif isinstance(required_columns, list):
        for field in sorted({str(item) for item in required_columns} - set(row_template)):
            missing.append(f"training_course_inventory_template.row_template.{field}")
    prefill_rows = template.get("prefill_rows")
    if not isinstance(prefill_rows, list):
        missing.append("training_course_inventory_template.prefill_rows")
    else:
        for index, row in enumerate(prefill_rows[:10], start=1):
            if not isinstance(row, dict):
                missing.append(f"training_course_inventory_template.prefill_rows.row_{index}")
                continue
            for field in ("source_type", "course_name", "course_goal", "review_state"):
                if field not in row:
                    missing.append(f"training_course_inventory_template.prefill_rows.row_{index}.{field}")
    gate = template.get("review_gate") if isinstance(template.get("review_gate"), dict) else {}
    if not gate:
        missing.append("training_course_inventory_template.review_gate")
    elif gate.get("approval_claim") is not False:
        missing.append("training_course_inventory_template.review_gate.approval_claim")
    return missing


def _missing_training_necessity_review_fields(payload: dict[str, Any]) -> list[str]:
    review = payload.get("training_necessity_review")
    if not isinstance(review, dict):
        return ["training_necessity_review"]
    missing: list[str] = []
    if review.get("schema") != "aihr_training_necessity_review_v1":
        missing.append(f"training_necessity_review.schema:{review.get('schema')}")
    for field in (
        "guide_stage",
        "status",
        "purpose",
        "target_population",
        "requested_constraints",
        "review_dimensions",
        "summary",
        "rows",
        "validation_rules",
        "review_gate",
    ):
        if field not in review:
            missing.append(f"training_necessity_review.{field}")
    summary = review.get("summary") if isinstance(review.get("summary"), dict) else {}
    for field in (
        "row_count",
        "review_required_rows",
        "approval_blocked_rows",
        "job_linkage_status_counts",
        "level_fit_status_counts",
        "delivery_feasibility_status_counts",
        "duplicate_or_generic_status_counts",
        "performance_contribution_status_counts",
        "required_optional_counts",
        "decision_state_counts",
    ):
        if field not in summary:
            missing.append(f"training_necessity_review.summary.{field}")
    rows = review.get("rows")
    if not isinstance(rows, list):
        missing.append("training_necessity_review.rows")
    else:
        required_row_fields = {
            "sequence",
            "course_name",
            "job_linkage",
            "level_fit",
            "required_optional_review",
            "duplicate_or_generic_review",
            "delivery_feasibility",
            "performance_contribution",
            "decision_state",
            "human_review",
            "review_flags",
            "recommended_review_action",
        }
        nested_required = {
            "job_linkage": {
                "status",
                "course_scope_relation",
                "course_scope_alignment",
                "evidence_directness",
                "task_ksa_basis_counts",
                "review_reason",
            },
            "level_fit": {"status", "target_level_band", "course_level", "review_reason"},
            "required_optional_review": {
                "code",
                "label",
                "rationale",
                "statutory_or_mandatory_basis",
                "approval_claim",
            },
            "duplicate_or_generic_review": {
                "status",
                "codes",
                "duplicate_or_generic_warning",
                "specificity_warning",
                "mapping_strength_warning",
            },
            "delivery_feasibility": {
                "status",
                "constraint_status",
                "hours",
                "methods",
                "facilities",
                "requested_constraints",
                "constraint_fit",
            },
            "performance_contribution": {
                "status",
                "evidence_chain_status",
                "gap_ksa",
                "training_goal_ksa",
                "covered_elements",
                "review_reason",
            },
        }
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                missing.append(f"training_necessity_review.rows.row_{index}")
                continue
            for field in sorted(required_row_fields - set(row)):
                missing.append(f"training_necessity_review.rows.row_{index}.{field}")
            for field, child_fields in nested_required.items():
                child = row.get(field)
                if not isinstance(child, dict):
                    missing.append(f"training_necessity_review.rows.row_{index}.{field}")
                    continue
                for child_field in sorted(child_fields - set(child)):
                    missing.append(
                        f"training_necessity_review.rows.row_{index}.{field}.{child_field}"
                    )
            required_optional = row.get("required_optional_review")
            if isinstance(required_optional, dict) and required_optional.get("approval_claim") is not False:
                missing.append(
                    f"training_necessity_review.rows.row_{index}.required_optional_review.approval_claim"
                )
    gate = review.get("review_gate") if isinstance(review.get("review_gate"), dict) else {}
    if not gate:
        missing.append("training_necessity_review.review_gate")
    elif gate.get("approval_claim") is not False:
        missing.append("training_necessity_review.review_gate.approval_claim")
    return missing


def _missing_annual_operation_plan_fields(payload: dict[str, Any]) -> list[str]:
    plan = payload.get("annual_operation_plan")
    if not isinstance(plan, dict):
        return ["annual_operation_plan"]
    missing: list[str] = []
    if plan.get("schema") != "aihr_annual_operation_plan_seed_v1":
        missing.append(f"annual_operation_plan.schema:{plan.get('schema')}")
    for field in (
        "guide_stage",
        "status",
        "purpose",
        "target_population",
        "requested_constraints",
        "summary",
        "rows",
        "review_gate",
        "export_fields",
    ):
        if field not in plan:
            missing.append(f"annual_operation_plan.{field}")
    review_gate = plan.get("review_gate") if isinstance(plan.get("review_gate"), dict) else {}
    if not review_gate:
        missing.append("annual_operation_plan.review_gate")
    else:
        if review_gate.get("approval_claim") is not False:
            missing.append("annual_operation_plan.review_gate.approval_claim")
        for field in ("status", "message"):
            if not review_gate.get(field):
                missing.append(f"annual_operation_plan.review_gate.{field}")
    rows = plan.get("rows")
    if not isinstance(rows, list):
        missing.append("annual_operation_plan.rows")
    else:
        required_row_fields = {
            "sequence",
            "recommended_window",
            "phase",
            "course_name",
            "need_classification",
            "decision_status",
            "human_review_severity",
            "evidence_chain_status",
            "constraint_status",
            "review_flags",
            "scheduling_rationale",
        }
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                missing.append(f"annual_operation_plan.row_{index}")
                continue
            for field in sorted(required_row_fields - set(row)):
                missing.append(f"annual_operation_plan.row_{index}.{field}")
    return missing


def _missing_query_route_fields(payload: dict[str, Any]) -> list[str]:
    route = payload.get("query_route")
    if not isinstance(route, dict):
        return ["query_route"]
    missing: list[str] = []
    if route.get("schema") != "ncs_query_route_v1":
        missing.append("query_route.schema")
    if route.get("tool") != "plan_ncs_education_path":
        missing.append(f"query_route.tool:{route.get('tool')}")
    if route.get("available") is not True:
        missing.append(f"query_route.available:{route.get('available')}")
    if not route.get("route_fingerprint"):
        missing.append("query_route.route_fingerprint")
    expected_chain = route.get("expected_tool_chain")
    if not isinstance(expected_chain, list) or "plan_ncs_education_path" not in expected_chain:
        missing.append("query_route.expected_tool_chain.plan_ncs_education_path")
    if not isinstance(expected_chain, list) or "recommend_training_transition" not in expected_chain:
        missing.append("query_route.expected_tool_chain.recommend_training_transition")
    contract = route.get("route_contract")
    if not isinstance(contract, dict):
        missing.append("query_route.route_contract")
    else:
        if contract.get("schema") != "ncs_query_route_v1":
            missing.append("query_route.route_contract.schema")
        if contract.get("route_first") is not True:
            missing.append("query_route.route_contract.route_first")
        if contract.get("primary_tool") != route.get("tool"):
            missing.append("query_route.route_contract.primary_tool")
        if contract.get("route_fingerprint") != route.get("route_fingerprint"):
            missing.append("query_route.route_contract.route_fingerprint")
    return missing


def _review_context_policy_issues(payload: dict[str, Any]) -> list[str]:
    policy = payload.get("review_context_policy")
    if not isinstance(policy, dict):
        return []
    issues: list[str] = []
    required_values = {
        "review_only": True,
        "non_scoring": True,
        "approval_claim": False,
        "db_writes": False,
        "status_update_allowed": False,
        "source_payload_exposed": False,
    }
    for key, expected in required_values.items():
        if policy.get(key) is not expected:
            issues.append(f"{key}:{policy.get(key)}")
    if policy.get("schema") != "aihr_public_review_context_policy_v1":
        issues.append("schema")
    for key in ("official_learning_module_rule", "ocr_report_context_rule"):
        if not policy.get(key):
            issues.append(key)
    return issues


def _contract_checks(payload: dict[str, Any]) -> list[tuple[str, bool, str]]:
    sensitive_scan_payload = {
        key: value
        for key, value in payload.items()
        if key != "review_context_policy"
    }
    text = json.dumps(sensitive_scan_payload, ensure_ascii=False)
    matrix = payload.get("training_system_matrix") or []
    guide_trace = (
        payload.get("training_system_guide_trace")
        if isinstance(payload.get("training_system_guide_trace"), dict)
        else {}
    )
    guide_checks = guide_trace.get("checks") if isinstance(guide_trace.get("checks"), list) else []
    guide_codes = {
        str(item.get("code"))
        for item in guide_checks
        if isinstance(item, dict) and item.get("code")
    }
    missing_guide_codes = sorted(REQUIRED_GUIDE_TRACE_CODES - guide_codes)
    guide_stages = guide_trace.get("guide_workflow_stages") if isinstance(guide_trace.get("guide_workflow_stages"), list) else []
    if not guide_stages:
        guide_workflow = guide_trace.get("guide_workflow") if isinstance(guide_trace.get("guide_workflow"), dict) else {}
        guide_stages = guide_workflow.get("steps") if isinstance(guide_workflow.get("steps"), list) else []
    guide_stage_codes = {
        str(item.get("code"))
        for item in guide_stages
        if isinstance(item, dict) and item.get("code")
    }
    missing_guide_stage_codes = sorted(REQUIRED_GUIDE_WORKFLOW_STAGE_CODES - guide_stage_codes)
    invalid_guide_statuses = [
        f"{item.get('code') or index}:{item.get('status')}"
        for index, item in enumerate(guide_checks, start=1)
        if isinstance(item, dict) and item.get("status") not in {"ready", "needs_review"}
    ]
    missing_guide_check_fields = [
        f"{item.get('code') or index}.{field}"
        for index, item in enumerate(guide_checks, start=1)
        if isinstance(item, dict)
        for field in ("code", "label", "status", "evidence")
        if field not in item
    ]
    invalid_guide_stage_statuses = [
        f"{item.get('code') or index}:{item.get('status')}"
        for index, item in enumerate(guide_stages, start=1)
        if isinstance(item, dict) and item.get("status") not in {"ready", "needs_review"}
    ]
    missing_delivery_rows: list[str] = []
    missing_matrix_contract_fields = _missing_matrix_contract_fields(matrix)
    missing_recommended_path_fields = _missing_recommended_path_fields(payload)
    missing_scope_baseline_fields = _missing_scope_baseline_fields(payload)
    missing_course_intake_requirements_fields = _missing_course_intake_requirements_fields(payload)
    missing_training_course_inventory_template_fields = _missing_training_course_inventory_template_fields(payload)
    missing_training_necessity_review_fields = _missing_training_necessity_review_fields(payload)
    missing_annual_operation_plan_fields = _missing_annual_operation_plan_fields(payload)
    missing_query_route_fields = _missing_query_route_fields(payload)
    review_context_policy_issues = _review_context_policy_issues(payload)
    review_context_policy_present = isinstance(payload.get("review_context_policy"), dict)
    for index, row in enumerate(matrix, start=1):
        if not isinstance(row, dict):
            continue
        missing = _missing_delivery_fit_fields(row)
        if missing:
            label = row.get("course_name") or row.get("rank") or index
            missing_delivery_rows.append(f"{label}: {', '.join(missing)}")
    leaked_markers = [
        marker
        for marker in SENSITIVE_DEMO_MARKERS
        if marker.lower() in text.lower()
    ]
    audit = payload.get("audit") or {}
    return [
        ("Plan view", payload.get("ok") is True and payload.get("view") == "ncs_education_plan", _esc(payload.get("view"))),
        ("Matrix", bool(matrix), f"{len(matrix)} rows"),
        (
            "Query route",
            not missing_query_route_fields,
            "route evidence present" if not missing_query_route_fields else ", ".join(missing_query_route_fields[:10]),
        ),
        (
            "Review context policy",
            review_context_policy_present and not review_context_policy_issues,
            "review-only/non-scoring public policy present"
            if review_context_policy_present and not review_context_policy_issues
            else "missing"
            if not review_context_policy_present
            else ", ".join(review_context_policy_issues[:10]),
        ),
        (
            "Recommended path",
            not missing_recommended_path_fields,
            "workflow stages present" if not missing_recommended_path_fields else ", ".join(missing_recommended_path_fields[:10]),
        ),
        (
            "Scope baseline",
            not missing_scope_baseline_fields,
            "job/NCS scope baseline present"
            if not missing_scope_baseline_fields
            else ", ".join(missing_scope_baseline_fields[:10]),
        ),
        (
            "Course intake requirements",
            not missing_course_intake_requirements_fields,
            "C1-1 course-investigation intake contract present"
            if not missing_course_intake_requirements_fields
            else ", ".join(missing_course_intake_requirements_fields[:10]),
        ),
        (
            "Training course inventory template",
            not missing_training_course_inventory_template_fields,
            "C1-1 inventory-table template present"
            if not missing_training_course_inventory_template_fields
            else ", ".join(missing_training_course_inventory_template_fields[:10]),
        ),
        (
            "Training necessity review",
            not missing_training_necessity_review_fields,
            "C1-2 necessity-review contract present"
            if not missing_training_necessity_review_fields
            else ", ".join(missing_training_necessity_review_fields[:10]),
        ),
        (
            "Annual operation plan seed",
            not missing_annual_operation_plan_fields,
            "C2-2 operation-plan seed present"
            if not missing_annual_operation_plan_fields
            else ", ".join(missing_annual_operation_plan_fields[:10]),
        ),
        (
            "Planner row contract",
            bool(matrix) and not missing_matrix_contract_fields,
            "all rows expose task/KSA, facility fit, and human review"
            if not missing_matrix_contract_fields
            else ", ".join(missing_matrix_contract_fields[:10]),
        ),
        (
            "Delivery fit",
            bool(matrix) and not missing_delivery_rows,
            "all rows expose level/hours/methods/facilities" if not missing_delivery_rows else "; ".join(missing_delivery_rows[:5]),
        ),
        (
            "Guide trace",
            (
                guide_trace.get("schema") == "aihr_training_system_guide_trace_v1"
                and not missing_guide_codes
                and not missing_guide_stage_codes
                and not missing_guide_check_fields
                and not invalid_guide_statuses
                and not invalid_guide_stage_statuses
            ),
            (
                "all required guide steps"
                if not missing_guide_codes
                and not missing_guide_stage_codes
                and not missing_guide_check_fields
                and not invalid_guide_statuses
                and not invalid_guide_stage_statuses
                else ", ".join(
                    [
                        *missing_guide_codes,
                        *missing_guide_stage_codes,
                        *missing_guide_check_fields,
                        *invalid_guide_statuses,
                        *invalid_guide_stage_statuses,
                    ]
                )
            ),
        ),
        ("SQF inactive", audit.get("sqf_used") is False, str(audit.get("sqf_used"))),
        ("Study modules inactive", audit.get("learning_modules_used") is False, str(audit.get("learning_modules_used"))),
        (
            "No sensitive payload markers",
            not leaked_markers,
            "hidden" if not leaked_markers else ", ".join(leaked_markers),
        ),
    ]


def _status_chip(ok: bool) -> str:
    label = "PASS" if ok else "CHECK"
    klass = "pass" if ok else "warn"
    return f'<span class="chip {klass}">{label}</span>'


def _render_contract(payload: dict[str, Any]) -> str:
    rows = []
    for label, ok, detail in _contract_checks(payload):
        rows.append(
            "<tr>"
            f"<td>{_esc(label)}</td>"
            f"<td>{_status_chip(ok)}</td>"
            f"<td>{detail}</td>"
            "</tr>"
        )
    return "<table class=\"contract\"><tbody>" + "".join(rows) + "</tbody></table>"


def _render_gaps(payload: dict[str, Any]) -> str:
    gaps = payload.get("priority_gaps") or []
    if not gaps:
        return "<p class=\"muted\">No gap KSA returned.</p>"
    return "<div class=\"tag-list\">" + "".join(f"<span>{_esc(gap)}</span>" for gap in gaps[:12]) + "</div>"


def _render_stage_courses(courses: Any) -> str:
    if not isinstance(courses, list) or not courses:
        return '<span class="muted">-</span>'
    rows = []
    for course in courses[:6]:
        if not isinstance(course, dict):
            rows.append(f"<li>{_esc(course)}</li>")
            continue
        fit = course.get("training_system_fit") if isinstance(course.get("training_system_fit"), dict) else {}
        need = fit.get("need_classification") if isinstance(fit.get("need_classification"), dict) else {}
        basis = fit.get("task_ksa_basis") if isinstance(fit.get("task_ksa_basis"), dict) else {}
        meta = [
            item
            for item in [
                course.get("tier_label") or course.get("tier"),
                f"{course.get('hours')}h" if course.get("hours") not in (None, "") else None,
                course.get("confidence_grade"),
            ]
            if item
        ]
        rationale = need.get("rationale") or course.get("rationale")
        rationale_html = f'<div class="muted">{_esc(rationale)}</div>' if rationale else ""
        rows.append(
            "<li>"
            f"<strong>{_esc(course.get('course_name') or course.get('training_course_id') or 'course')}</strong>"
            f"<div class=\"muted\">{_join(meta)}</div>"
            f"<div><span class=\"subtle-label\">required_optional</span>{_esc(need.get('label') or need.get('code') or '-')}</div>"
            f"{rationale_html}"
            f"<div><span class=\"subtle-label\">basis_types</span>{_join(basis.get('basis_types'))}</div>"
            f"<div><span class=\"subtle-label\">covered_elements</span>{_join(basis.get('covered_elements'))}</div>"
            "</li>"
        )
    if len(courses) > 6:
        rows.append(f'<li class="muted">+{len(courses) - 6} more courses</li>')
    return f'<ul class="path-courses">{"".join(rows)}</ul>'


def _render_recommended_path(payload: dict[str, Any]) -> str:
    stages = payload.get("recommended_path") or []
    if not isinstance(stages, list) or not stages:
        return "<p class=\"muted\">No recommended_path stages returned.</p>"
    rows = []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        guide_evidence = (
            stage.get("guide_stage_evidence")
            if isinstance(stage.get("guide_stage_evidence"), dict)
            else {}
        )
        details = [
            f"<p><strong>{_esc(stage.get('title') or '')}</strong></p>" if stage.get("title") else "",
            (
                '<div class="cell-block"><span class="subtle-label">guide_stage</span>'
                f"{_esc(stage.get('guide_stage'))} {_status_label(stage.get('guide_stage_status'))}"
                f"<p class=\"muted\">{_esc(guide_evidence.get('evidence'))}</p>"
                "</div>"
                if stage.get("guide_stage")
                else ""
            ),
            (
                f"<p><span class=\"subtle-label\">selection_rule</span>{_esc(stage.get('selection_rule'))}</p>"
                if stage.get("selection_rule")
                else ""
            ),
            (
                '<div class="cell-block"><span class="subtle-label">actions</span>'
                f"{_render_bullets(stage.get('actions'))}</div>"
                if stage.get("actions")
                else ""
            ),
            (
                '<div class="cell-block"><span class="subtle-label">priority_gaps</span>'
                f"{_render_bullets(stage.get('priority_gaps'))}</div>"
                if stage.get("priority_gaps")
                else ""
            ),
            (
                '<div class="cell-block"><span class="subtle-label">constraints</span>'
                f"{_render_dict_brief(stage.get('constraints'))}</div>"
                if stage.get("constraints")
                else ""
            ),
            (
                '<div class="cell-block"><span class="subtle-label">evidence_basis</span>'
                f"{_render_bullets(stage.get('evidence_basis'))}</div>"
                if stage.get("evidence_basis")
                else ""
            ),
            (
                '<div class="cell-block"><span class="subtle-label">outputs</span>'
                f"{_render_bullets(stage.get('outputs'))}</div>"
                if stage.get("outputs")
                else ""
            ),
        ]
        rows.append(
            "<tr>"
            f"<td>{_esc(stage.get('stage'))}</td>"
            f"<td><strong>{_esc(stage.get('role'))}</strong></td>"
            f"<td>{''.join(details)}</td>"
            f"<td>{_render_stage_courses(stage.get('courses'))}</td>"
            "</tr>"
        )
    return (
        '<table class="path"><thead><tr>'
        "<th>Stage</th><th>Role</th><th>Workflow Evidence</th><th>Courses</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _render_query_route(payload: dict[str, Any]) -> str:
    route = payload.get("query_route") if isinstance(payload.get("query_route"), dict) else {}
    if not route:
        return "<p class=\"muted\">No query route evidence returned.</p>"
    contract = route.get("route_contract") if isinstance(route.get("route_contract"), dict) else {}
    flags = [
        flag.get("code")
        for flag in route.get("guard_flags", [])
        if isinstance(flag, dict) and flag.get("code")
    ]
    return (
        "<table class=\"contract\"><tbody>"
        f"<tr><td>schema</td><td colspan=\"2\">{_esc(route.get('schema'))}</td></tr>"
        f"<tr><td>scenario</td><td>{_esc(route.get('scenario'))}</td><td>{_esc(route.get('tool'))}</td></tr>"
        f"<tr><td>available</td><td>{_esc(route.get('available'))}</td><td>{_esc(route.get('confidence'))}</td></tr>"
        f"<tr><td>expected tool chain</td><td colspan=\"2\">{_join(route.get('expected_tool_chain'))}</td></tr>"
        f"<tr><td>route fingerprint</td><td colspan=\"2\">{_esc(route.get('route_fingerprint'))}</td></tr>"
        f"<tr><td>route contract</td><td>{_esc(contract.get('primary_tool'))}</td><td>route_first={_esc(contract.get('route_first'))}</td></tr>"
        f"<tr><td>guard flags</td><td colspan=\"2\">{_join(flags)}</td></tr>"
        "</tbody></table>"
    )


def _render_training_summary(payload: dict[str, Any]) -> str:
    summary = payload.get("training_system_summary") or {}
    if not summary:
        return "<p class=\"muted\">No training-system summary returned.</p>"
    need_counts = summary.get("need_classification_counts") or {}
    directness_counts = summary.get("evidence_directness_counts") or {}
    education_source_counts = summary.get("education_type_evidence_source_counts") or {}
    delivery_operation_counts = summary.get("delivery_operation_counts") or {}
    delivery_constraint_counts = summary.get("delivery_constraint_fit_counts") or {}
    return (
        "<table class=\"contract\"><tbody>"
        f"<tr><td>Courses / 과정 수</td><td>{_esc(summary.get('course_count'))}</td><td>{_join(summary.get('required_course_names'))}</td></tr>"
        f"<tr><td>Need counts / 필요도 분류</td><td colspan=\"2\">{_esc(need_counts)}</td></tr>"
        f"<tr><td>Evidence counts / 근거 직접성</td><td colspan=\"2\">{_esc(directness_counts)}</td></tr>"
        f"<tr><td>Education type evidence</td><td colspan=\"2\">{_esc(education_source_counts)}</td></tr>"
        f"<tr><td>Delivery operation counts</td><td colspan=\"2\">{_esc(delivery_operation_counts)}</td></tr>"
        f"<tr><td>Delivery constraint fit</td><td colspan=\"2\">{_esc(delivery_constraint_counts)}</td></tr>"
        f"<tr><td>Review candidates / 검토 후보</td><td colspan=\"2\">{_join(summary.get('review_required_course_names'))}</td></tr>"
        "</tbody></table>"
    )


def _render_annual_operation_plan(payload: dict[str, Any]) -> str:
    plan = payload.get("annual_operation_plan") if isinstance(payload.get("annual_operation_plan"), dict) else {}
    if not plan:
        return "<p class=\"muted\">No annual_operation_plan seed returned.</p>"
    summary = plan.get("summary") if isinstance(plan.get("summary"), dict) else {}
    review_gate = plan.get("review_gate") if isinstance(plan.get("review_gate"), dict) else {}
    rows: list[str] = []
    for row in plan.get("rows") or []:
        if not isinstance(row, dict):
            continue
        rows.append(
            "<tr>"
            f"<td>{_esc(row.get('sequence'))}</td>"
            f"<td>{_esc(row.get('recommended_window'))}<br><span class=\"muted\">{_esc(row.get('phase'))}</span></td>"
            f"<td><strong>{_esc(row.get('course_name'))}</strong><br><span class=\"muted\">{_esc(row.get('training_course_id'))}</span></td>"
            f"<td>{_esc(row.get('need_classification'))}<br><span class=\"muted\">{_esc(row.get('system_suggestion'))}</span></td>"
            f"<td>{_esc(row.get('hours'))}h<br>{_join(row.get('methods'))}<br>{_join(row.get('facilities'))}</td>"
            f"<td>{_esc(row.get('constraint_status'))}<br><span class=\"muted\">method={_esc(row.get('method_status'))}, time={_esc(row.get('time_status'))}, facility={_esc(row.get('facility_status'))}</span></td>"
            f"<td>{_esc(row.get('decision_status'))}<br><span class=\"muted\">review={_esc(row.get('human_review_severity'))}, chain={_esc(row.get('evidence_chain_status'))}</span></td>"
            f"<td>{_join(row.get('review_flags'))}<br><span class=\"muted\">{_esc(row.get('scheduling_rationale'))}</span></td>"
            "</tr>"
        )
    return (
        "<table class=\"contract\"><tbody>"
        f"<tr><td>annual_operation_plan</td><td>{_esc(plan.get('schema'))}</td><td>{_status_label(plan.get('status'))}</td></tr>"
        f"<tr><td>target population</td><td>{_esc(plan.get('target_population'))}</td><td>{_esc(summary)}</td></tr>"
        f"<tr><td>review gate</td><td>{_esc(review_gate.get('status'))}</td><td>approval_claim={_esc(review_gate.get('approval_claim'))}</td></tr>"
        f"<tr><td>purpose</td><td colspan=\"2\">{_esc(plan.get('purpose'))}</td></tr>"
        "</tbody></table>"
        "<table class=\"matrix\"><thead><tr>"
        "<th>#</th><th>Window / Phase</th><th>Course</th><th>Need</th><th>Hours / Delivery</th><th>Constraint Fit</th><th>Decision</th><th>Review</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _render_course_intake_requirements(payload: dict[str, Any]) -> str:
    intake = (
        payload.get("course_intake_requirements")
        if isinstance(payload.get("course_intake_requirements"), dict)
        else {}
    )
    if not intake:
        return "<p class=\"muted\">No course_intake_requirements returned.</p>"
    policy = intake.get("mapping_policy") if isinstance(intake.get("mapping_policy"), dict) else {}
    gate = intake.get("review_gate") if isinstance(intake.get("review_gate"), dict) else {}
    prefill = (
        intake.get("prefill_from_recommendations")
        if isinstance(intake.get("prefill_from_recommendations"), dict)
        else {}
    )
    rows: list[str] = []
    for item in intake.get("required_fields") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            "<tr>"
            f"<td>{_esc(item.get('field'))}</td>"
            f"<td>{_esc(item.get('purpose'))}</td>"
            f"<td>{_join(item.get('maps_to'))}</td>"
            "</tr>"
        )
    return (
        "<table class=\"contract\"><tbody>"
        f"<tr><td>course_intake_requirements</td><td>{_esc(intake.get('schema'))}</td><td>{_status_label(intake.get('status'))}</td></tr>"
        f"<tr><td>target scope</td><td>{_esc(intake.get('current_scope'))} -> {_esc(intake.get('target_scope'))}</td><td>{_esc(intake.get('target_population'))}</td></tr>"
        f"<tr><td>mapping policy</td><td>title_only_allowed={_esc(policy.get('title_only_mapping_allowed'))}</td><td>framework_reference_is_not_scoring_source={_esc(policy.get('framework_reference_is_not_scoring_source'))}</td></tr>"
        f"<tr><td>prefill</td><td colspan=\"2\">{_esc(prefill)}</td></tr>"
        f"<tr><td>review gate</td><td>{_esc(gate.get('status'))}</td><td>approval_claim={_esc(gate.get('approval_claim'))}</td></tr>"
        f"<tr><td>purpose</td><td colspan=\"2\">{_esc(intake.get('purpose'))}</td></tr>"
        "</tbody></table>"
        "<table class=\"matrix\"><thead><tr><th>Required field</th><th>Why collected</th><th>Maps to</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _render_training_course_inventory_template(payload: dict[str, Any]) -> str:
    template = (
        payload.get("training_course_inventory_template")
        if isinstance(payload.get("training_course_inventory_template"), dict)
        else {}
    )
    if not template:
        return "<p class=\"muted\">No training_course_inventory_template returned.</p>"
    gate = template.get("review_gate") if isinstance(template.get("review_gate"), dict) else {}
    column_rows: list[str] = []
    for item in template.get("columns") or []:
        if not isinstance(item, dict):
            continue
        column_rows.append(
            "<tr>"
            f"<td>{_esc(item.get('column'))}</td>"
            f"<td>{_esc(item.get('required'))}</td>"
            f"<td>{_esc(item.get('purpose'))}</td>"
            f"<td>{_join(item.get('maps_to'))}</td>"
            "</tr>"
        )
    prefill_rows: list[str] = []
    for row in template.get("prefill_rows") or []:
        if not isinstance(row, dict):
            continue
        prefill_rows.append(
            "<tr>"
            f"<td>{_esc(row.get('source_type'))}</td>"
            f"<td>{_esc(row.get('course_name'))}</td>"
            f"<td>{_esc(row.get('course_goal'))}</td>"
            f"<td>{_join(row.get('ksa_evidence'))}</td>"
            f"<td>{_esc(row.get('hours'))}h / {_join(row.get('methods'))}</td>"
            f"<td>{_esc(row.get('duplicate_or_generic_risk'))}</td>"
            f"<td>{_esc(row.get('review_state'))}</td>"
            "</tr>"
        )
    return (
        "<table class=\"contract\"><tbody>"
        f"<tr><td>training_course_inventory_template</td><td>{_esc(template.get('schema'))}</td><td>{_status_label(template.get('status'))}</td></tr>"
        f"<tr><td>target population</td><td>{_esc(template.get('target_population'))}</td><td>required_columns={len(template.get('required_columns') or [])}</td></tr>"
        f"<tr><td>review gate</td><td>{_esc(gate.get('status'))}</td><td>approval_claim={_esc(gate.get('approval_claim'))}</td></tr>"
        f"<tr><td>purpose</td><td colspan=\"2\">{_esc(template.get('purpose'))}</td></tr>"
        "</tbody></table>"
        "<table class=\"matrix\"><thead><tr><th>Column</th><th>Required</th><th>Why collected</th><th>Maps to</th></tr></thead><tbody>"
        + "".join(column_rows)
        + "</tbody></table>"
        "<table class=\"matrix\"><thead><tr><th>Source</th><th>Course</th><th>Goal</th><th>KSA</th><th>Hours / Method</th><th>Risk</th><th>Review</th></tr></thead><tbody>"
        + "".join(prefill_rows)
        + "</tbody></table>"
    )


def _render_training_necessity_review(payload: dict[str, Any]) -> str:
    review = (
        payload.get("training_necessity_review")
        if isinstance(payload.get("training_necessity_review"), dict)
        else {}
    )
    if not review:
        return "<p class=\"muted\">No training_necessity_review returned.</p>"
    summary = review.get("summary") if isinstance(review.get("summary"), dict) else {}
    gate = review.get("review_gate") if isinstance(review.get("review_gate"), dict) else {}
    rows: list[str] = []
    for row in review.get("rows") or []:
        if not isinstance(row, dict):
            continue
        job_linkage = row.get("job_linkage") if isinstance(row.get("job_linkage"), dict) else {}
        level_fit = row.get("level_fit") if isinstance(row.get("level_fit"), dict) else {}
        need = (
            row.get("required_optional_review")
            if isinstance(row.get("required_optional_review"), dict)
            else {}
        )
        risk = (
            row.get("duplicate_or_generic_review")
            if isinstance(row.get("duplicate_or_generic_review"), dict)
            else {}
        )
        delivery = (
            row.get("delivery_feasibility")
            if isinstance(row.get("delivery_feasibility"), dict)
            else {}
        )
        contribution = (
            row.get("performance_contribution")
            if isinstance(row.get("performance_contribution"), dict)
            else {}
        )
        decision = row.get("decision_state") if isinstance(row.get("decision_state"), dict) else {}
        rows.append(
            "<tr>"
            f"<td>{_esc(row.get('sequence'))}</td>"
            f"<td>{_esc(row.get('course_name'))}<br><span class=\"muted\">{_esc(row.get('training_course_id'))}</span></td>"
            f"<td>{_status_label(job_linkage.get('status'))}<br><span class=\"muted\">{_esc(job_linkage.get('course_scope_relation'))} / {_esc(job_linkage.get('evidence_directness'))}</span></td>"
            f"<td>{_status_label(level_fit.get('status'))}<br><span class=\"muted\">level={_esc(level_fit.get('course_level'))}</span></td>"
            f"<td>{_esc(need.get('code'))}<br><span class=\"muted\">approval_claim={_esc(need.get('approval_claim'))}</span></td>"
            f"<td>{_status_label(risk.get('status'))}<br><span class=\"muted\">{_join(risk.get('codes'))}</span></td>"
            f"<td>{_status_label(delivery.get('status'))}<br><span class=\"muted\">{_esc(delivery.get('constraint_status'))}</span></td>"
            f"<td>{_status_label(contribution.get('status'))}<br><span class=\"muted\">chain={_esc(contribution.get('evidence_chain_status'))}</span></td>"
            f"<td>{_esc(decision.get('status'))}<br><span class=\"muted\">{_esc(row.get('recommended_review_action'))}</span></td>"
            "</tr>"
        )
    return (
        "<table class=\"contract\"><tbody>"
        f"<tr><td>training_necessity_review</td><td>{_esc(review.get('schema'))}</td><td>{_status_label(review.get('status'))}</td></tr>"
        f"<tr><td>guide stage</td><td>{_esc(review.get('guide_stage'))}</td><td>approval_claim={_esc(gate.get('approval_claim'))}</td></tr>"
        f"<tr><td>summary</td><td colspan=\"2\">rows={_esc(summary.get('row_count'))}, review_required={_esc(summary.get('review_required_rows'))}, approval_blocked={_esc(summary.get('approval_blocked_rows'))}</td></tr>"
        f"<tr><td>purpose</td><td colspan=\"2\">{_esc(review.get('purpose'))}</td></tr>"
        "</tbody></table>"
        "<table class=\"matrix\"><thead><tr><th>#</th><th>Course</th><th>Job Linkage</th><th>Level</th><th>Req/Opt</th><th>Risk</th><th>Delivery</th><th>Performance</th><th>Decision</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _render_scope_baseline(payload: dict[str, Any]) -> str:
    baseline = payload.get("scope_baseline") if isinstance(payload.get("scope_baseline"), dict) else {}
    if not baseline:
        return "<p class=\"muted\">No scope_baseline returned.</p>"
    current = baseline.get("current") if isinstance(baseline.get("current"), dict) else {}
    target = baseline.get("target") if isinstance(baseline.get("target"), dict) else {}
    review = baseline.get("human_review") if isinstance(baseline.get("human_review"), dict) else {}
    components = baseline.get("adjusted_transferability_components")
    return (
        "<table class=\"contract\"><tbody>"
        f"<tr><td>scope_baseline</td><td colspan=\"3\">{_esc(baseline.get('schema'))}</td></tr>"
        f"<tr><td>purpose</td><td colspan=\"3\">{_esc(baseline.get('purpose'))}</td></tr>"
        f"<tr><td>current</td><td>{_esc(current.get('requested_query'))}</td><td>{_esc(current.get('resolved_scope'))}</td><td>{_esc(current.get('match_level'))}</td></tr>"
        f"<tr><td>target</td><td>{_esc(target.get('requested_query'))}</td><td>{_esc(target.get('resolved_scope'))}</td><td>{_esc(target.get('match_level'))}</td></tr>"
        f"<tr><td>unit counts</td><td>{_esc(current.get('unit_count'))}</td><td>{_esc(target.get('unit_count'))}</td><td>{_esc(baseline.get('ncs_scope_relation'))}</td></tr>"
        f"<tr><td>exact / adjusted</td><td>{_esc(baseline.get('exact_ksa_overlap_ratio'))}</td><td>{_esc(baseline.get('ontology_adjusted_transferability_ratio'))}</td><td>{_esc(components)}</td></tr>"
        f"<tr><td>scope review</td><td>{_status_label(review.get('status'))}</td><td colspan=\"2\">{_join(review.get('flags'))}</td></tr>"
        "</tbody></table>"
    )


def _review_gate_summary(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("training_system_matrix") if isinstance(payload.get("training_system_matrix"), list) else []
    summary = payload.get("training_system_summary") if isinstance(payload.get("training_system_summary"), dict) else {}
    trace = (
        payload.get("training_system_guide_trace")
        if isinstance(payload.get("training_system_guide_trace"), dict)
        else {}
    )
    guide_items: list[str] = []
    for field in ("guide_workflow_stages", "checks"):
        values = trace.get(field) if isinstance(trace.get(field), list) else []
        for item in values:
            if isinstance(item, dict) and item.get("status") == "needs_review":
                guide_items.append(str(item.get("code") or item.get("label") or "guide_item"))

    review_course_names: list[str] = []
    adjacent_course_names: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        course_name = str(row.get("course_name") or row.get("training_course_id") or "course")
        review = row.get("human_review") if isinstance(row.get("human_review"), dict) else {}
        flags = review.get("flags") or row.get("review_flags") or []
        severity = str(review.get("severity") or "").strip()
        if flags or severity not in {"", "ready", "clear", "pass"}:
            review_course_names.append(course_name)
        need = row.get("need_classification") if isinstance(row.get("need_classification"), dict) else {}
        basis = row.get("required_optional_basis") if isinstance(row.get("required_optional_basis"), dict) else {}
        need_code = str(basis.get("code") or need.get("code") or "")
        if need_code == "adjacent_reference":
            adjacent_course_names.append(course_name)

    summary_review_names = [
        str(item)
        for item in summary.get("review_required_course_names") or []
        if str(item).strip()
    ]
    for item in summary_review_names:
        if item not in review_course_names:
            review_course_names.append(item)

    needs_review = bool(review_course_names or guide_items or adjacent_course_names)
    return {
        "status": "needs_review" if needs_review else "ready",
        "guide_items": sorted(set(guide_items)),
        "review_course_names": review_course_names,
        "adjacent_course_names": adjacent_course_names,
        "message": (
            "Contract evidence is present, but content still needs human review before use."
            if needs_review
            else "Contract evidence is present and no content review gate was detected."
        ),
    }


def _render_review_gate_notice(payload: dict[str, Any]) -> str:
    gate = _review_gate_summary(payload)
    status = gate.get("status")
    klass = "warn" if status == "needs_review" else "pass"
    return (
        f'<div class="review-gate {klass}">'
        "<div>"
        '<span class="subtle-label">content_readiness</span>'
        f"<h3>Review Gate / Content Readiness {_status_label(status)}</h3>"
        f"<p>{_esc(gate.get('message'))}</p>"
        "</div>"
        "<table class=\"contract\"><tbody>"
        f"<tr><td>Guide items needing review</td><td>{_join(gate.get('guide_items'))}</td></tr>"
        f"<tr><td>Course rows needing review</td><td>{_join(gate.get('review_course_names'))}</td></tr>"
        f"<tr><td>Adjacent reference rows</td><td>{_join(gate.get('adjacent_course_names'))}</td></tr>"
        "</tbody></table>"
        "</div>"
    )


def _render_guide_trace(payload: dict[str, Any]) -> str:
    trace = payload.get("training_system_guide_trace") or {}
    if not trace:
        return "<p class=\"muted\">No guide trace returned.</p>"
    stages = trace.get("guide_workflow_stages")
    if not isinstance(stages, list):
        workflow = trace.get("guide_workflow") if isinstance(trace.get("guide_workflow"), dict) else {}
        stages = workflow.get("steps") if isinstance(workflow.get("steps"), list) else []
    checks = trace.get("checks") if isinstance(trace.get("checks"), list) else []
    rows = []
    for item in stages:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        klass = "pass" if status == "ready" else "warn"
        rows.append(
            "<tr>"
            f"<td>{_esc(item.get('code'))}</td>"
            f"<td><span class=\"chip {klass}\">{_esc(status)}</span></td>"
            f"<td>{_esc(item.get('title'))}</td>"
            f"<td>{_esc(item.get('evidence'))}</td>"
            "</tr>"
        )
    for item in checks:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        klass = "pass" if status == "ready" else "warn"
        rows.append(
            "<tr>"
            f"<td>{_esc(item.get('code'))}</td>"
            f"<td><span class=\"chip {klass}\">{_esc(status)}</span></td>"
            f"<td>{_esc(item.get('label'))}</td>"
            f"<td>{_esc(item.get('evidence'))}</td>"
            "</tr>"
        )
    return (
        "<table class=\"contract\"><tbody>"
        f"<tr><td>Schema</td><td colspan=\"3\">{_esc(trace.get('schema'))}</td></tr>"
        f"<tr><td>Rubric</td><td colspan=\"3\">{_esc(trace.get('rubric_source'))} / {_esc(trace.get('rubric_role'))}</td></tr>"
        f"<tr><td>Policy</td><td colspan=\"3\">{_esc(trace.get('non_source_data_policy'))}</td></tr>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _render_required_optional(item: dict[str, Any]) -> str:
    need = item.get("need_classification") if isinstance(item.get("need_classification"), dict) else {}
    basis = item.get("required_optional_basis") if isinstance(item.get("required_optional_basis"), dict) else {}
    code = basis.get("code") or need.get("code") or "unknown"
    label = basis.get("label") or need.get("label") or code
    rationales = []
    for source in (basis, need):
        rationale = source.get("rationale") if isinstance(source, dict) else None
        if rationale and rationale not in rationales:
            rationales.append(rationale)
    return (
        f"<strong>{_esc(label)}</strong><br>"
        f'<span class="muted">required_optional_basis: {_esc(code)}</span>'
        + "".join(f'<p class="muted">{_esc(rationale)}</p>' for rationale in rationales)
    )


def _render_task_ksa_basis(basis: dict[str, Any]) -> str:
    if not isinstance(basis, dict) or not basis:
        return '<span class="muted">-</span>'
    return (
        _field("basis_types", basis.get("basis_types"), limit=8)
        + _field(
            "counts",
            [
                f"target={basis.get('target_scope_ksa_count', len(basis.get('target_scope_ksa') or []))}",
                f"gap={basis.get('gap_ksa_count', len(basis.get('gap_ksa') or []))}",
                f"goal={basis.get('training_goal_ksa_count', len(basis.get('training_goal_ksa') or []))}",
                f"elements={basis.get('covered_element_count', len(basis.get('covered_elements') or []))}",
            ],
            limit=4,
        )
        + _field("covered_elements", basis.get("covered_elements"), limit=4)
        + _field("gap_ksa", basis.get("gap_ksa"), limit=4)
        + _field("training_goal_ksa", basis.get("training_goal_ksa"), limit=4)
        + _field("target_scope_ksa", basis.get("target_scope_ksa"), limit=4)
    )


def _render_evidence_chain(item: dict[str, Any]) -> str:
    chain = item.get("evidence_chain") if isinstance(item.get("evidence_chain"), dict) else {}
    if not chain:
        return '<span class="muted">-</span>'
    completeness = chain.get("completeness") if isinstance(chain.get("completeness"), dict) else {}
    links = chain.get("links") if isinstance(chain.get("links"), list) else []
    rendered_links = []
    for link in links[:5]:
        if not isinstance(link, dict):
            continue
        value = link.get("value")
        if isinstance(value, list):
            value_text = ", ".join(str(item) for item in value[:4])
        else:
            value_text = str(value or "")
        rendered_links.append(f"{link.get('stage')}: {value_text}")
    return (
        '<div class="cell-block"><span class="subtle-label">evidence_chain</span>'
        f"{_status_label(completeness.get('status') or 'partial')}</div>"
        f'<div class="cell-block"><span class="subtle-label">schema</span>{_esc(chain.get("schema") or "")}</div>'
        + _field("chain", rendered_links, limit=5)
        + _field("missing", completeness.get("missing_stages") or [], limit=5)
    )


def _render_course_link(item: dict[str, Any]) -> str:
    link = item.get("course_link") if isinstance(item.get("course_link"), dict) else {}
    if not link:
        return ""
    direct = link.get("evidence_directness") if isinstance(link.get("evidence_directness"), dict) else {}
    scope_fit = link.get("course_scope_fit") if isinstance(link.get("course_scope_fit"), dict) else {}
    strength = link.get("mapping_strength") if isinstance(link.get("mapping_strength"), dict) else {}
    return (
        '<div class="cell-block"><span class="subtle-label">course_link</span>'
        f"{_esc(direct.get('label') or direct.get('code') or 'unknown')}</div>"
        + (
            '<div class="cell-block"><span class="subtle-label">mapping_strength</span>'
            f"target={_esc(strength.get('target_scope_ksa_count'))}, "
            f"gap={_esc(strength.get('gap_ksa_count'))}, "
            f"goal={_esc(strength.get('training_goal_ksa_count'))}, "
            f"elements={_esc(strength.get('covered_element_count'))}</div>"
            if strength
            else ""
        )
        + _field("chain", link.get("mapping_chain"), limit=5)
        + (
            '<div class="cell-block"><span class="subtle-label">course_scope_fit</span>'
            f"{_esc(scope_fit.get('relation') or 'unknown')}</div>"
            if scope_fit
            else ""
        )
    )


def _render_course_scope_fit(item: dict[str, Any]) -> str:
    fit = item.get("course_scope_fit") if isinstance(item.get("course_scope_fit"), dict) else {}
    if not fit:
        return '<span class="muted">-</span>'
    return (
        f"<strong>{_esc(fit.get('label') or fit.get('relation') or 'unknown')}</strong><br>"
        f'<span class="muted">relation: {_esc(fit.get("relation") or "unknown")}</span><br>'
        f'<span class="muted">alignment: {_esc(fit.get("alignment") or "unknown")}</span>'
        + _field("fields", fit.get("fields"), limit=4)
        + _field("direct_units", fit.get("direct_unit_codes"), limit=3)
    )


def _render_delivery_fit(fit: dict[str, Any], delivery_operation: dict[str, Any]) -> str:
    method_fit = (
        delivery_operation.get("method_constraint_fit")
        if isinstance(delivery_operation.get("method_constraint_fit"), dict)
        else {}
    )
    time_fit = (
        delivery_operation.get("time_constraint_fit")
        if isinstance(delivery_operation.get("time_constraint_fit"), dict)
        else {}
    )
    constraint_fit = (
        delivery_operation.get("constraint_fit")
        if isinstance(delivery_operation.get("constraint_fit"), dict)
        else {}
    )
    return (
        f'<div class="cell-block"><span class="subtle-label">level</span>{_esc(fit.get("level") or "-")}</div>'
        f'<div class="cell-block"><span class="subtle-label">hours</span>{_esc(fit.get("hours") or "-")}</div>'
        f'<div class="cell-block"><span class="subtle-label">methods</span>{_join(fit.get("methods"))}</div>'
        f'<div class="cell-block"><span class="subtle-label">facilities</span>{_join(fit.get("facilities"))}</div>'
        f'<div class="cell-block"><span class="subtle-label">delivery_operation</span>{_esc(delivery_operation.get("code") or "-")}</div>'
        f'<div class="cell-block"><span class="subtle-label">method_fit</span>{_status_label(method_fit.get("status") or "not_requested")}</div>'
        f'<div class="cell-block"><span class="subtle-label">time_fit</span>{_status_label(time_fit.get("status") or "not_requested")}</div>'
        f'<div class="cell-block"><span class="subtle-label">constraint_fit</span>{_status_label(constraint_fit.get("status") or "not_requested")}</div>'
    )


def _row_facility_fit(item: dict[str, Any]) -> dict[str, Any]:
    fit = item.get("facility_constraint_fit")
    if isinstance(fit, dict) and fit:
        return fit
    delivery = item.get("delivery_operation") if isinstance(item.get("delivery_operation"), dict) else {}
    nested = delivery.get("facility_constraint_fit")
    if isinstance(nested, dict):
        return nested
    return {}


def _render_facility_fit(item: dict[str, Any]) -> str:
    fit = _row_facility_fit(item)
    if not fit:
        return '<span class="muted">-</span>'
    return (
        '<div class="cell-block"><span class="subtle-label">facility_constraint_fit</span>'
        f"{_status_label(fit.get('status'))}</div>"
        + _field("requested", fit.get("requested"), limit=4)
        + _field("available", fit.get("available"), limit=4)
        + _field("matched", fit.get("matched"), limit=4)
        + _field("missing", fit.get("missing"), limit=4)
        + (f'<p class="muted">{_esc(fit.get("rationale"))}</p>' if fit.get("rationale") else "")
    )


def _render_human_review(item: dict[str, Any]) -> str:
    review = item.get("human_review") if isinstance(item.get("human_review"), dict) else {}
    flags = review.get("flags") or item.get("review_flags") or []
    if not review and not flags:
        return '<span class="muted">-</span>'
    severity = review.get("severity") or ("needs_review" if flags else "ready")
    prompt = review.get("prompt")
    return (
        '<div class="cell-block"><span class="subtle-label">human_review</span>'
        f"{_status_label(severity)}</div>"
        + (f'<p>{_esc(prompt)}</p>' if prompt else "")
        + _field("flags", flags, limit=6)
    )


def _render_decision_state(item: dict[str, Any]) -> str:
    state = item.get("decision_state") if isinstance(item.get("decision_state"), dict) else {}
    if not state:
        return '<span class="muted">-</span>'
    return (
        '<div class="cell-block"><span class="subtle-label">decision_state</span>'
        f"{_status_label(state.get('status') or 'pending_human_decision')}</div>"
        f'<div class="cell-block"><span class="subtle-label">system_suggestion</span>{_esc(state.get("system_suggestion") or "unknown")}</div>'
        f'<div class="cell-block"><span class="subtle-label">approval_claim</span>{_esc(state.get("approval_claim"))}</div>'
        + _field("allowed", state.get("allowed_decisions"), limit=6)
        + (f'<p class="muted">{_esc(state.get("message"))}</p>' if state.get("message") else "")
    )


def _render_warning_block(item: dict[str, Any]) -> str:
    specificity = item.get("specificity_warning") if isinstance(item.get("specificity_warning"), dict) else {}
    duplicate = (
        item.get("duplicate_or_generic_warning")
        if isinstance(item.get("duplicate_or_generic_warning"), dict)
        else {}
    )
    mapping_strength = (
        item.get("mapping_strength_warning")
        if isinstance(item.get("mapping_strength_warning"), dict)
        else {}
    )
    return (
        '<div class="cell-block"><span class="subtle-label">specificity_warning</span>'
        f"{_status_label(specificity.get('status') or 'clear')}</div>"
        + _field("specificity", specificity.get("codes") or [], limit=4)
        + '<div class="cell-block"><span class="subtle-label">duplicate_or_generic_warning</span>'
        f"{_status_label(duplicate.get('status') or 'clear')}</div>"
        + _field("duplicate/generic", duplicate.get("codes") or [], limit=4)
        + '<div class="cell-block"><span class="subtle-label">mapping_strength_warning</span>'
        f"{_status_label(mapping_strength.get('status') or 'clear')}</div>"
        + _field("mapping strength", mapping_strength.get("codes") or [], limit=4)
    )


def _render_matrix(payload: dict[str, Any]) -> str:
    rows = []
    for item in payload.get("training_system_matrix") or []:
        need = item.get("need_classification") or {}
        direct = item.get("evidence_directness") or {}
        basis = item.get("task_ksa_basis") or {}
        fit = item.get("course_fit") or {}
        delivery_operation = item.get("delivery_operation") or {}
        job_scope = item.get("job_scope") or {}
        level_band = item.get("target_level_band") or {}
        grouping = item.get("planner_grouping") or {}
        rows.append(
            "<tr>"
            f"<td>{_esc(item.get('rank'))}</td>"
            f"<td>{_esc(job_scope.get('transition') or grouping.get('job_scope') or '')}</td>"
            f"<td><strong>{_esc(item.get('course_name'))}</strong><br><span class=\"muted\">{_esc(item.get('training_course_id'))}</span></td>"
            f"<td>{_esc(need.get('label') or need.get('code'))}<br><span class=\"muted\">{_esc(need.get('code'))}</span></td>"
            f"<td>{_render_required_optional(item)}</td>"
            f"<td>{_esc(direct.get('label') or direct.get('code'))}<br><span class=\"muted\">{_esc(direct.get('code'))}</span>{_render_course_link(item)}</td>"
            f"<td>{_render_course_scope_fit(item)}</td>"
            f"<td>{_render_evidence_chain(item)}</td>"
            f"<td>{_render_task_ksa_basis(basis)}</td>"
            f"<td>{_esc(level_band.get('label') or 'unknown')}<br>{_render_delivery_fit(fit, delivery_operation)}</td>"
            f"<td>{_render_facility_fit(item)}</td>"
            f"<td>{_render_warning_block(item)}</td>"
            f"<td>{_render_decision_state(item)}</td>"
            f"<td>{_render_human_review(item)}</td>"
            "</tr>"
        )
    return (
        "<table class=\"matrix\"><thead><tr>"
        "<th>Rank</th><th>Job Scope</th><th>Course</th><th>Need</th><th>Required / Optional</th><th>Evidence</th><th>Course Scope Fit</th><th>Evidence Chain</th><th>Task/KSA Basis</th><th>Level / Delivery</th><th>Facility Fit</th><th>Warnings</th><th>Decision State</th><th>Human Review</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _render_plan(
    payload: dict[str, Any],
    source: Path,
    review_contexts: list[tuple[Path, dict[str, Any]]] | None = None,
) -> str:
    current = payload.get("current_scope") or {}
    target = payload.get("target_scope") or {}
    scenario = payload.get("scenario") or {}
    transition = payload.get("transition_assessment") or {}
    caveats = payload.get("caveats") or []
    public_notice = payload.get("public_demo_notice")
    return f"""
    <section class="demo">
      <header class="demo-header">
        <div>
          <p class="eyebrow">{_esc(source.name)}</p>
          <h2>{_esc(payload.get('plan_objective'))}</h2>
          <p class="scope">{_esc(current.get('resolved_as'))} -> {_esc(target.get('resolved_as'))}</p>
          {f'<p class="prototype-notice">{_esc(public_notice)}</p>' if public_notice else ''}
        </div>
        <div class="scenario">
          <span>{_esc((scenario.get('title') or scenario.get('selected')))}</span>
          <strong>{_esc(payload.get('target_population'))}</strong>
        </div>
      </header>
      <div class="summary-grid">
        <div>
          <h3>전환 진단</h3>
          <p>{_esc(transition.get('summary'))}</p>
          <p class="rubric-note">2026 인사담당자 NCS 활용 교육훈련체계 구축 가이드는 직무-과업-KSA-훈련과정 연결을 점검하는 루브릭으로만 사용됩니다.</p>
        </div>
        <div>
          <h3>Contract</h3>
          {_render_contract(payload)}
        </div>
      </div>
      {_render_review_gate_notice(payload)}
      {_render_review_context_annex(payload, review_contexts or [])}
      <h3>Priority Gap KSA / 우선 보완 KSA</h3>
      {_render_gaps(payload)}
      <h3>Query Route / MCP Routing Evidence</h3>
      {_render_query_route(payload)}
      <h3>Scope Baseline / C1-1 Job Scope Evidence</h3>
      {_render_scope_baseline(payload)}
      <h3>Course Intake Requirements / C1-1 Course Investigation</h3>
      {_render_course_intake_requirements(payload)}
      <h3>Training Course Inventory Template / C1-1 Course Investigation Table</h3>
      {_render_training_course_inventory_template(payload)}
      <h3>Training Necessity Review / C1-2</h3>
      {_render_training_necessity_review(payload)}
      <h3>Recommended Path / Workflow Evidence</h3>
      {_render_recommended_path(payload)}
      <h3>2026 Guide Trace / AI-HR Rubric</h3>
      {_render_guide_trace(payload)}
      <h3>Training-System Summary / 교육훈련체계 요약</h3>
      {_render_training_summary(payload)}
      <h3>Annual Operation Plan Seed / C2-2</h3>
      {_render_annual_operation_plan(payload)}
      <h3>Training-System Matrix / 교육훈련체계 매트릭스</h3>
      {_render_matrix(payload)}
      <h3>Caveats / 검토 유의사항</h3>
      <ul class="caveats">
        {''.join(f'<li>{_esc(caveat)}</li>' for caveat in caveats)}
      </ul>
    </section>
    """


def render(
    paths: list[Path],
    *,
    review_context_paths: list[Path] | None = None,
) -> str:
    demos = [(path, public_demo_payload(_load_json(path))) for path in paths]
    review_contexts = [
        (path, public_demo_payload(_load_json(path)))
        for path in (review_context_paths or [])
    ]
    sections = "\n".join(_render_plan(payload, path, review_contexts) for path, payload in demos)
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI-HR 교육훈련체계 시제품 데모 | AI-HR Education Plan Demo</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #1f2933;
      --muted: #5b6572;
      --line: #d5dbe3;
      --paper: #f7f8fa;
      --surface: #ffffff;
      --accent: #0f766e;
      --warn: #b45309;
      --pass: #166534;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Arial, "Malgun Gothic", sans-serif;
      color: var(--ink);
      background: var(--paper);
      line-height: 1.45;
    }}
    main {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 24px;
    }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-end;
      margin-bottom: 20px;
      border-bottom: 2px solid var(--line);
      padding-bottom: 16px;
    }}
    h1, h2, h3, p {{ margin-top: 0; }}
    h1 {{ font-size: 28px; margin-bottom: 6px; }}
    h2 {{ font-size: 22px; margin-bottom: 6px; }}
    h3 {{ font-size: 15px; margin: 18px 0 8px; }}
    .muted, .eyebrow {{ color: var(--muted); }}
    .eyebrow {{ font-size: 12px; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 4px; }}
    .demo {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      margin-bottom: 18px;
    }}
    .demo-header, .summary-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(260px, 360px);
      gap: 16px;
    }}
    .scope {{ font-weight: 700; color: var(--accent); margin-bottom: 0; }}
    .scenario {{
      border-left: 4px solid var(--accent);
      padding-left: 12px;
      align-self: start;
    }}
    .scenario span, .scenario strong {{ display: block; }}
    .rubric-note {{
      margin: 10px 0 0;
      padding: 8px 10px;
      border-left: 4px solid var(--accent);
      background: #eef7f5;
      color: var(--muted);
      font-size: 13px;
    }}
    .prototype-notice {{
      margin: 8px 0 0;
      font-weight: 700;
      color: var(--warn);
    }}
    .review-gate {{
      border: 1px solid var(--line);
      border-left: 6px solid var(--warn);
      display: grid;
      grid-template-columns: minmax(0, 0.85fr) minmax(280px, 1.15fr);
      gap: 16px;
      margin: 18px 0;
      padding: 14px;
      background: #fffaf3;
    }}
    .review-gate.pass {{
      background: #f0fdf4;
      border-left-color: var(--pass);
    }}
    .review-gate h3 {{
      margin: 2px 0 8px;
    }}
    .review-context {{
      margin: 18px 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-left: 6px solid var(--warn);
      background: #fffaf3;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      background: var(--surface);
    }}
    th, td {{
      border: 1px solid var(--line);
      padding: 8px;
      text-align: left;
      vertical-align: top;
      overflow-wrap: anywhere;
      font-size: 13px;
    }}
    th {{ background: #eef2f5; font-weight: 700; }}
    .contract td:first-child {{ width: 42%; }}
    .chip {{
      display: inline-block;
      min-width: 52px;
      text-align: center;
      padding: 2px 6px;
      border-radius: 999px;
      color: #fff;
      font-size: 11px;
      font-weight: 700;
    }}
    .chip.pass {{ background: var(--pass); }}
    .chip.neutral {{ background: var(--line); color: var(--ink); }}
    .chip.warn {{ background: var(--warn); }}
    .tag-list {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .tag-list span {{
      border: 1px solid var(--line);
      background: #f2f6f4;
      padding: 5px 8px;
      border-radius: 999px;
      font-size: 12px;
    }}
    .cell-block {{ margin: 0 0 7px; }}
    .subtle-label {{
      display: block;
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      margin-bottom: 2px;
    }}
    .mini-list, .path-courses {{
      margin: 3px 0 0 16px;
      padding: 0;
    }}
    .mini-list li, .path-courses li {{ margin-bottom: 4px; }}
    .kv-list {{
      display: grid;
      gap: 3px;
    }}
    .path td:first-child {{ width: 64px; }}
    .path td:nth-child(2) {{ width: 180px; }}
    .caveats {{ padding-left: 18px; }}
    @media (max-width: 820px) {{
      main {{ padding: 12px; }}
      .topbar, .demo-header, .summary-grid {{ grid-template-columns: 1fr; }}
      table {{ display: block; overflow-x: auto; }}
      th, td {{ min-width: 130px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header class="topbar">
      <div>
        <p class="eyebrow">NCS_MCP prototype</p>
        <h1>AI-HR 교육훈련체계 시제품 데모</h1>
        <p class="muted">AI-HR 교육훈련체계 데모</p>
        <p class="muted">AI-HR Education Plan Demo</p>
        <p class="prototype-notice">시제품 검증 화면이며 공식 승인, 자격 인정, 법적 적격성 판단 화면이 아닙니다.</p>
        <p class="muted">2026 인사담당자 NCS 활용 교육훈련체계 구축 가이드는 계획 수립 루브릭으로 사용하며, 원천 훈련 데이터로 적재하지 않습니다.</p>
      </div>
      <p class="muted">{len(paths)}개 JSON 산출물에서 생성</p>
    </header>
    {sections}
  </main>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--review-context",
        action="append",
        default=[],
        type=Path,
        help="Optional Human Review sidecar JSON such as OCR cards, unit evidence packet, or official learning-module gap audit.",
    )
    parser.add_argument("json_paths", nargs="+", type=Path)
    args = parser.parse_args()
    args.out.write_text(
        render(args.json_paths, review_context_paths=args.review_context),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "out": str(args.out),
                "inputs": [str(path) for path in args.json_paths],
                "review_contexts": [str(path) for path in args.review_context],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
