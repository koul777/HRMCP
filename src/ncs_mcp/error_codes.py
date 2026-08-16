from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ErrorCodeSpec:
    code: str
    category: str
    retryable: bool
    severity: str
    description: str


ERROR_CODE_CATALOG: dict[str, ErrorCodeSpec] = {
    "NOT_FOUND": ErrorCodeSpec("NOT_FOUND", "not_found", False, "info", "Requested record was not found."),
    "TASK_NOT_FOUND": ErrorCodeSpec("TASK_NOT_FOUND", "not_found", False, "info", "Requested task was not found."),
    "concept_not_found": ErrorCodeSpec("concept_not_found", "not_found", False, "info", "Ontology concept was not found."),
    "sqf_target_not_found": ErrorCodeSpec("sqf_target_not_found", "not_found", False, "info", "Legacy SQF target was not found."),
    "sqf_ncs_match_not_found": ErrorCodeSpec("sqf_ncs_match_not_found", "not_found", False, "info", "Legacy SQF-NCS match was not found."),
    "sqf_job_level_not_found": ErrorCodeSpec("sqf_job_level_not_found", "not_found", False, "info", "Legacy SQF job level was not found."),
    "learning_module_ncs_link_not_found": ErrorCodeSpec("learning_module_ncs_link_not_found", "not_found", False, "info", "Legacy learning-module link was not found."),
    "training_goal_concept_link_not_found": ErrorCodeSpec("training_goal_concept_link_not_found", "not_found", False, "info", "Training-goal concept link was not found."),
    "task_ksa_concept_relation_not_found": ErrorCodeSpec("task_ksa_concept_relation_not_found", "not_found", False, "info", "Task-KSA concept relation was not found."),
    "ncs_unit_not_found": ErrorCodeSpec("ncs_unit_not_found", "not_found", False, "info", "NCS competency unit was not found."),
    "learning_module_not_found": ErrorCodeSpec("learning_module_not_found", "not_found", False, "info", "Legacy learning module was not found."),
    "missing_task_locator": ErrorCodeSpec("missing_task_locator", "validation", False, "warn", "A criteria id, unit code, or query is required."),
    "missing_transition_query": ErrorCodeSpec("missing_transition_query", "validation", False, "warn", "Current and target transition queries are required."),
    "missing_columns": ErrorCodeSpec("missing_columns", "validation", False, "warn", "Required CSV columns are missing."),
    "missing_input_csv": ErrorCodeSpec("missing_input_csv", "validation", False, "warn", "Input CSV path is required."),
    "concept_name_required": ErrorCodeSpec("concept_name_required", "validation", False, "warn", "Concept name is required for this update."),
    "low_quality_query": ErrorCodeSpec("low_quality_query", "validation", False, "warn", "Query is too short or ambiguous for ranking."),
    "invalid_tool_parameters": ErrorCodeSpec("invalid_tool_parameters", "validation", False, "warn", "Tool arguments did not match the expected schema."),
    "invalid_review_triage_input": ErrorCodeSpec("invalid_review_triage_input", "validation", False, "warn", "Review triage input is malformed."),
    "unsupported_analysis_mode": ErrorCodeSpec("unsupported_analysis_mode", "unsupported", False, "warn", "Analysis mode is not supported."),
    "unsupported_mode": ErrorCodeSpec("unsupported_mode", "unsupported", False, "warn", "Requested mode is not supported."),
    "unsupported_target_type": ErrorCodeSpec("unsupported_target_type", "unsupported", False, "warn", "Requested target type is not supported."),
    "unsupported_review_status": ErrorCodeSpec("unsupported_review_status", "unsupported", False, "warn", "Review status is not supported."),
    "unsupported_trust_mode": ErrorCodeSpec("unsupported_trust_mode", "unsupported", False, "warn", "Trust mode is not supported."),
    "unsupported_concept_type": ErrorCodeSpec("unsupported_concept_type", "unsupported", False, "warn", "Concept type is not supported."),
    "meta_tool_recursion_blocked": ErrorCodeSpec("meta_tool_recursion_blocked", "policy", False, "warn", "Meta tools cannot recursively execute meta tools."),
    "tool_not_executable_via_meta": ErrorCodeSpec("tool_not_executable_via_meta", "policy", False, "warn", "Tool is blocked from meta execution."),
    "tool_execution_failed": ErrorCodeSpec("tool_execution_failed", "execution", False, "error", "Tool handler failed during execution."),
    "qualification_service_key_missing": ErrorCodeSpec("qualification_service_key_missing", "configuration", False, "error", "Qualification API service key is missing."),
    "job_base_service_key_missing": ErrorCodeSpec("job_base_service_key_missing", "configuration", False, "error", "Job-base API service key is missing."),
    "qualification_collection_scope_required": ErrorCodeSpec("qualification_collection_scope_required", "validation", False, "warn", "Qualification collection scope is required."),
    "external_api_error": ErrorCodeSpec("external_api_error", "external_dependency", True, "error", "External API call failed."),
}


def error_metadata(code: str) -> dict[str, object]:
    spec = ERROR_CODE_CATALOG.get(code)
    known = spec is not None
    if spec is None:
        lower = code.lower()
        upper = code.upper()
        if "not_found" in lower or upper in {"NOT_FOUND", "TASK_NOT_FOUND"}:
            spec = ErrorCodeSpec(code, "not_found", False, "info", "Requested record was not found.")
        elif lower.startswith(("missing_", "invalid_")):
            spec = ErrorCodeSpec(code, "validation", False, "warn", "Request input failed validation.")
        elif lower.startswith("unsupported_"):
            spec = ErrorCodeSpec(code, "unsupported", False, "warn", "Requested operation is not supported.")
        elif lower.endswith("_service_key_missing"):
            spec = ErrorCodeSpec(code, "configuration", False, "error", "Required service key is missing.")
        else:
            spec = ErrorCodeSpec(code, "application", False, "error", "Application error.")
    metadata = asdict(spec)
    metadata["known"] = known
    metadata.pop("code", None)
    return metadata
