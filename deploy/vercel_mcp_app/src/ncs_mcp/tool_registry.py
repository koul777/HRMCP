from __future__ import annotations

from typing import Any


NCS_TOOL_PROFILES: dict[str, dict[str, Any]] = {
    "ncs_discover_tools": {
        "description": "Find the right compact NCS MCP tool for a user intent.",
        "aliases": ["discover", "tool catalog", "help", "what can this MCP do"],
    },
    "ncs_execute_tool": {
        "description": "Execute a discovered read-only user tool by name and params.",
        "aliases": ["execute", "run tool", "meta tool"],
    },
    "ncs_search": {
        "description": "Search NCS classifications, units, elements, criteria, and KSA records.",
        "aliases": ["search", "classification", "unit", "criteria", "KSA"],
    },
    "ncs_unit_detail": {
        "description": "Return one NCS competency unit with selected criteria, KSA, training, and qualification evidence.",
        "aliases": ["unit detail", "competency unit", "elements", "criteria"],
    },
    "ncs_training": {
        "description": "Search cached NCS training courses or return one course by id.",
        "aliases": ["training", "course", "\ud6c8\ub828", "\uad50\uc721"],
    },
    "ncs_analysis": {
        "description": "Read career path, qualification, job-base, or ontology evidence.",
        "aliases": ["career path", "qualification", "job base", "ontology", "evidence"],
    },
    "recommend_training_for_task": {
        "description": "Recommend training courses for a current NCS task from KSA evidence.",
        "aliases": ["task training recommendation", "upskilling", "reskilling"],
    },
    "recommend_training_transition": {
        "description": "Recommend training for moving from one NCS scope or task query to another.",
        "aliases": ["transition", "career move", "gap training", "\uc9c1\ubb34 \uc804\ud658"],
    },
    "plan_ncs_education_path": {
        "description": (
            "Build a compact NCS education-system plan for a task or career transition, "
            "including training_system_summary, training_system_matrix, required/reference "
            "classification, KSA evidence, preferred facility constraints, and level/hour/method/facility fit."
        ),
        "aliases": [
            "education plan",
            "curriculum",
            "training path",
            "AI-HR plan",
            "training system matrix",
            "\uad50\uc721\uccb4\uacc4",
            "\uad50\uc721\uacc4\ud68d",
            "\ud6c8\ub828\uccb4\uacc4\ub3c4",
        ],
    },
    "recommend_task_transitions": {
        "description": "Recommend nearby NCS tasks from task/KSA similarity.",
        "aliases": ["task transition", "similar task", "transferability"],
    },
    "get_concept_evidence": {
        "description": "Return source KSA, criteria, relation, and recommendation evidence for one ontology concept.",
        "aliases": ["concept", "KSA evidence", "ontology evidence"],
    },
    "get_quality_issues": {
        "description": "Inspect quality issues for NCS records and ontology links.",
        "aliases": ["quality", "diagnostics", "issues"],
    },
    "review_learning_module_ncs_link": {
        "description": "Operator tool for reviewing legacy learning-module links.",
        "aliases": ["review learning module"],
    },
    "review_training_goal_concept_link": {
        "description": "Operator tool for reviewing training-goal to KSA concept links that affect recommendations.",
        "aliases": ["review training goal", "training goal link", "human review"],
    },
    "review_task_ksa_concept_relation": {
        "description": "Operator tool for reviewing task-KSA concept relations used in transition reasoning.",
        "aliases": ["review task KSA", "task relation", "human review"],
    },
    "review_ontology_concept": {
        "description": "Operator tool for human-reviewing ontology concepts without changing raw KSA text.",
        "aliases": ["review concept", "human review"],
    },
}


NCS_TOOL_CATEGORIES: dict[str, list[str]] = {
    "tool_discovery": ["ncs_discover_tools", "ncs_execute_tool"],
    "ncs_structure_search": ["ncs_search", "ncs_unit_detail"],
    "training_courses": [
        "ncs_training",
        "recommend_training_for_task",
        "recommend_training_transition",
        "plan_ncs_education_path",
    ],
    "career_transition": ["recommend_task_transitions", "recommend_training_transition", "plan_ncs_education_path"],
    "education_planning": ["plan_ncs_education_path", "recommend_training_transition", "recommend_training_for_task"],
    "evidence_analysis": ["ncs_analysis", "get_concept_evidence", "get_quality_issues"],
    "operator_review": [
        "review_training_goal_concept_link",
        "review_task_ksa_concept_relation",
        "review_ontology_concept",
        "review_learning_module_ncs_link",
    ],
}


NCS_TOOL_ALIASES: dict[str, list[str]] = {
    "tool_discovery": ["help", "discover", "tools", "catalog"],
    "ncs_structure_search": ["NCS", "classification", "unit", "KSA", "\ub2a5\ub825\ub2e8\uc704"],
    "training_courses": ["training", "course", "education", "\ud6c8\ub828", "\uad50\uc721"],
    "career_transition": [
        "transition",
        "career",
        "reskilling",
        "upskilling",
        "\uc9c1\ubb34\uc804\ud658",
        "\uacbd\ub825\uac1c\ubc1c",
    ],
    "education_planning": [
        "education plan",
        "curriculum",
        "learning path",
        "training path",
        "\uad50\uc721\uccb4\uacc4",
        "\uad50\uc721\uacc4\ud68d",
    ],
    "evidence_analysis": ["evidence", "qualification", "career path", "job base", "ontology"],
    "operator_review": ["review", "quality", "human review", "\uac80\ud1a0"],
}


NCS_META_READ_ONLY_SAVE_FORCED_TOOLS = {
    "recommend_training_for_task",
    "recommend_training_transition",
    "plan_ncs_education_path",
}


NCS_META_COMPACT_DEFAULT_TOOLS = {
    "recommend_training_for_task",
    "recommend_training_transition",
}


NCS_EXECUTABLE_TOOL_NAMES = {
    "ncs_search",
    "ncs_unit_detail",
    "ncs_training",
    "ncs_analysis",
    "recommend_training_for_task",
    "recommend_training_transition",
    "plan_ncs_education_path",
    "recommend_task_transitions",
    "get_concept_evidence",
}


USER_MCP_TOOLS = {
    "ncs_discover_tools",
    "ncs_execute_tool",
    "ncs_search",
    "ncs_unit_detail",
    "ncs_training",
    "ncs_analysis",
    "recommend_training_for_task",
    "recommend_training_transition",
    "plan_ncs_education_path",
    "recommend_task_transitions",
    "get_concept_evidence",
}


OPERATOR_MCP_TOOLS = {
    "get_quality_issues",
    "review_training_goal_concept_link",
    "review_task_ksa_concept_relation",
    "review_learning_module_ncs_link",
    "review_ontology_concept",
}


# Advanced ontology / education-integration / transition tools. These are still
# maturing (education-path planning and transition recommendations have known
# issues), so they are hidden from the public MCP surface unless
# NCS_MCP_ENABLE_ADVANCED_TOOLS=1 is set. The stable core (search, unit detail,
# training lookup, evidence analysis, task-training recommendation) stays exposed.
ADVANCED_MCP_TOOLS = {
    "plan_ncs_education_path",
    "recommend_training_transition",
    "recommend_task_transitions",
    "get_concept_evidence",
}


LEGACY_MCP_TOOLS = {
    "list_classifications",
    "get_competency_units",
    "search_ncs_units",
    "get_unit_structure",
    "get_element_detail",
    "get_performance_criteria",
    "get_ksa",
    "search_ncs",
    "resolve_ncs_query_scope",
    "search_training_courses",
    "get_training_course",
    "get_career_path_summary",
    "search_qualification_items",
    "search_job_base_competencies",
    "compare_raw_refined",
    "get_api_join_status",
    "build_training_course_ontology_links",
    "prepare_ontology_review_queue",
    "import_career_paths",
    "query_qualification_items",
    "collect_qualification_items",
    "get_qualification_error_report",
    "retry_qualification_errors",
    "query_job_base_competencies",
    "collect_job_base_competencies",
    "import_ncs_reference_html",
    "import_ncs_reference_docx",
    "extract_ncs_reference_entities",
    "link_reference_entities_to_ncs",
    "get_sqf_duties",
    "search_sqf_jobs",
    "get_sqf_job_level",
    "build_sqf_ncs_mapping_candidates",
    "map_sqf_to_ncs",
    "analyze_gap",
    "recommend_next_ncs_units",
    "explain_mapping",
    "search_learning_modules",
    "get_learning_module",
    "get_learning_path_for_sqf_job",
    "recommend_education_for_duty",
    "recommend_learning_modules_by_ncs",
    "review_exact_learning_module_name_links",
    "build_ncs_derived_learning_plans",
    "build_report_training_courses",
    "recommend_education_by_concepts",
    "search_ncs_reference_chunks",
    "review_sqf_ncs_match",
    "get_sqf_ontology_summary",
    "search_sqf_document_chunks",
    "search_sqf_precision_matches",
    "get_sqf_ontology_job_level",
}


PUBLIC_MCP_TOOLS = USER_MCP_TOOLS
ADMIN_MCP_TOOLS = USER_MCP_TOOLS | OPERATOR_MCP_TOOLS

# Backward-compatible name for the default public MCP surface.
ACTIVE_MCP_TOOLS = PUBLIC_MCP_TOOLS
ALL_SUPPORTED_MCP_TOOLS = ADMIN_MCP_TOOLS


def discover_tools_for_intent(
    intent: str,
    *,
    executable_tool_names: set[str] | None = None,
    available_tool_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    query = " ".join((intent or "").lower().split())
    executable = executable_tool_names or set()
    available = available_tool_names or ALL_SUPPORTED_MCP_TOOLS
    matches: list[dict[str, Any]] = []
    for category, tool_names in NCS_TOOL_CATEGORIES.items():
        aliases = [category, *NCS_TOOL_ALIASES.get(category, [])]
        category_match = not query or any(query in alias.lower() or alias.lower() in query for alias in aliases)
        tool_matches: list[str] = []
        for tool_name in tool_names:
            if tool_name not in available:
                continue
            profile = NCS_TOOL_PROFILES.get(tool_name, {})
            haystack = " ".join(
                [
                    tool_name,
                    str(profile.get("description", "")),
                    " ".join(profile.get("aliases", [])),
                ]
            ).lower()
            if category_match or (query and query in haystack):
                tool_matches.append(tool_name)
        if tool_matches:
            matches.append(
                {
                    "category": category,
                    "tools": [
                        {
                            "name": tool_name,
                            "description": NCS_TOOL_PROFILES.get(tool_name, {}).get("description", ""),
                            "execution": "direct_or_ncs_execute_tool"
                            if tool_name in executable
                            else "direct_only",
                        }
                        for tool_name in tool_matches
                    ],
                }
            )
    return matches


def mcp_tools_for_mode(
    *,
    operator_tools_enabled: bool = False,
    advanced_tools_enabled: bool = True,
) -> set[str]:
    tools = ADMIN_MCP_TOOLS if operator_tools_enabled else PUBLIC_MCP_TOOLS
    if not advanced_tools_enabled:
        tools = tools - ADVANCED_MCP_TOOLS
    return tools


def executable_tool_names_for_mode(*, advanced_tools_enabled: bool = True) -> set[str]:
    if advanced_tools_enabled:
        return set(NCS_EXECUTABLE_TOOL_NAMES)
    return NCS_EXECUTABLE_TOOL_NAMES - ADVANCED_MCP_TOOLS
