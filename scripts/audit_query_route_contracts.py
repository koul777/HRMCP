from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.contracts import PLAN_NCS_EDUCATION_PATH_TOOL, QUERY_ROUTE_SCHEMA
from ncs_mcp.query_router import route_ncs_query


DEFAULT_OUT = PROJECT_ROOT / "reports" / "query_route_contract_audit_20260629.json"
DEFAULT_MARKDOWN_OUT = PROJECT_ROOT / "reports" / "query_route_contract_audit_20260629.md"
SAVE_FORCED_TOOLS = {
    "recommend_training_for_task",
    "recommend_training_transition",
    PLAN_NCS_EDUCATION_PATH_TOOL,
}


@dataclass(frozen=True)
class RouteAuditCase:
    name: str
    query: str
    expected_scenario: str
    expected_tool: str
    available_tool_names: set[str] | None = None
    expected_available: bool = True
    expected_missing_params: tuple[str, ...] | None = ()
    expected_guard_codes: tuple[str, ...] = ()
    expected_risk_codes: tuple[str, ...] = ()
    require_guide_prompt_template: bool = False
    require_operator_policy: bool = False
    require_save_forced_false: bool | None = None
    require_compact_default_true: bool | None = None
    note: str = ""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def default_cases() -> list[RouteAuditCase]:
    public_tools = {
        "ncs_search",
        "ncs_analysis",
        "ncs_training",
        "recommend_task_transitions",
        "recommend_training_for_task",
        "recommend_training_transition",
        PLAN_NCS_EDUCATION_PATH_TOOL,
        "get_concept_evidence",
    }
    operator_tools = {
        *public_tools,
        "get_quality_issues",
        "review_training_goal_concept_link",
    }
    return [
        RouteAuditCase(
            name="education_system_design",
            query="노무관리에서 인사기획으로 교육훈련체계 만들어줘",
            expected_scenario="education_system_design",
            expected_tool=PLAN_NCS_EDUCATION_PATH_TOOL,
            available_tool_names=public_tools,
            require_guide_prompt_template=True,
            require_save_forced_false=True,
            note="AI-HR education-system facade must expose route contract and no-save policy.",
        ),
        RouteAuditCase(
            name="training_transition",
            query="노무관리에서 인사기획 직무 전환 추천",
            expected_scenario="training_transition",
            expected_tool="recommend_training_transition",
            available_tool_names=public_tools,
            require_save_forced_false=True,
            require_compact_default_true=True,
            note="Transition recommendation meta execution must stay read-only by default.",
        ),
        RouteAuditCase(
            name="task_training",
            query="인력채용 훈련과정 추천",
            expected_scenario="task_training",
            expected_tool="recommend_training_for_task",
            available_tool_names=public_tools,
            require_guide_prompt_template=True,
            require_save_forced_false=True,
            require_compact_default_true=True,
            note="Task training route must keep compact/no-save recommendation defaults.",
        ),
        RouteAuditCase(
            name="structure_search",
            query="인사기획 NCS search",
            expected_scenario="structure_search",
            expected_tool="ncs_search",
            available_tool_names=public_tools,
            require_save_forced_false=False,
            note="Direct NCS search intent should not be swallowed by planning signals.",
        ),
        RouteAuditCase(
            name="operator_review",
            query="훈련목표 KSA 링크 품질 이슈를 검토해야 한다",
            expected_scenario="operator_review",
            expected_tool="get_quality_issues",
            available_tool_names=operator_tools,
            expected_guard_codes=("operator_review_route",),
            require_operator_policy=True,
            require_save_forced_false=False,
            note="Operator review routes are observable but not meta-executable approval paths.",
        ),
        RouteAuditCase(
            name="operator_review_ksa_definition_target",
            query="KSA 정의 검토와 human review 대상을 운영자가 확인하고 싶다",
            expected_scenario="operator_review",
            expected_tool="get_quality_issues",
            available_tool_names=operator_tools,
            expected_guard_codes=("operator_review_route",),
            require_operator_policy=True,
            require_save_forced_false=False,
            note=(
                "KSA definition/human-review target prompts must not be swallowed by "
                "task-training or education-system signals."
            ),
        ),
        RouteAuditCase(
            name="official_claim_risk",
            query="공식 승인 자격 인정 근거 분석",
            expected_scenario="evidence_analysis",
            expected_tool="ncs_analysis",
            available_tool_names=public_tools,
            expected_risk_codes=("official_or_legal_claim_risk",),
            require_save_forced_false=False,
            note="Official approval or legal eligibility language must be risk-flagged.",
        ),
    ]


def parse_case(value: str) -> RouteAuditCase:
    parts = [part.strip() for part in value.split("|")]
    if len(parts) not in {3, 4}:
        raise argparse.ArgumentTypeError(
            "--case must use 'query|expected_scenario|expected_tool' "
            "or 'name|query|expected_scenario|expected_tool'"
        )
    if len(parts) == 3:
        query, expected_scenario, expected_tool = parts
        name = expected_scenario
    else:
        name, query, expected_scenario, expected_tool = parts
    if not query or not expected_scenario or not expected_tool:
        raise argparse.ArgumentTypeError("--case fields must be non-empty")
    return RouteAuditCase(
        name=name or expected_scenario,
        query=query,
        expected_scenario=expected_scenario,
        expected_tool=expected_tool,
        expected_missing_params=None,
        require_save_forced_false=expected_tool in SAVE_FORCED_TOOLS,
    )


def audit_routes(cases: list[RouteAuditCase]) -> dict[str, Any]:
    rows = [audit_case(case) for case in cases]
    failures = [row for row in rows if row["status"] != "pass"]
    return {
        "schema": "ncs_query_route_contract_audit_v1",
        "generated_at": now_iso(),
        "ok": not failures,
        "status": "pass" if not failures else "fail",
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "case_count": len(rows),
        "pass_count": len(rows) - len(failures),
        "failure_count": len(failures),
        "rows": rows,
        "failure_summary": [
            {
                "name": row["name"],
                "query": row["query"],
                "issues": row["issues"],
            }
            for row in failures
        ],
    }


def audit_case(case: RouteAuditCase) -> dict[str, Any]:
    route = route_ncs_query(case.query, available_tool_names=case.available_tool_names)
    issues = _route_issues(case, route)
    contract = route.get("route_contract") if isinstance(route.get("route_contract"), dict) else {}
    execution_policy = (
        contract.get("execution_policy") if isinstance(contract.get("execution_policy"), dict) else {}
    )
    guard_codes = sorted(
        str(flag.get("code") or "")
        for flag in route.get("guard_flags") or []
        if isinstance(flag, dict) and flag.get("code")
    )
    risk_codes = sorted(
        str(flag.get("code") or "")
        for flag in route.get("risk_flags") or []
        if isinstance(flag, dict) and flag.get("code")
    )
    return {
        "name": case.name,
        "query": case.query,
        "status": "pass" if not issues else "fail",
        "issues": issues,
        "expected": {
            "scenario": case.expected_scenario,
            "tool": case.expected_tool,
            "available": case.expected_available,
            "missing_params": list(case.expected_missing_params)
            if case.expected_missing_params is not None
            else None,
            "guard_codes": list(case.expected_guard_codes),
            "risk_codes": list(case.expected_risk_codes),
        },
        "actual": {
            "schema": route.get("schema"),
            "scenario": route.get("scenario"),
            "tool": route.get("tool"),
            "available": route.get("available"),
            "missing_params": route.get("missing_params") or [],
            "guard_codes": guard_codes,
            "risk_codes": risk_codes,
            "expected_tool_chain": route.get("expected_tool_chain") or [],
            "route_fingerprint": route.get("route_fingerprint"),
            "contract_fingerprint": contract.get("route_fingerprint"),
            "execution_policy": execution_policy,
            "guide_prompt_template_id": (route.get("guide_prompt_template") or {}).get("id")
            if isinstance(route.get("guide_prompt_template"), dict)
            else None,
        },
        "note": case.note,
    }


def _route_issues(case: RouteAuditCase, route: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    contract = route.get("route_contract")
    if not isinstance(contract, dict):
        contract = {}
        issues.append("missing_route_contract")
    execution_policy = contract.get("execution_policy")
    if not isinstance(execution_policy, dict):
        execution_policy = {}
        issues.append("missing_execution_policy")

    if route.get("schema") != QUERY_ROUTE_SCHEMA:
        issues.append(f"schema:{route.get('schema')!r}")
    if contract.get("schema") != QUERY_ROUTE_SCHEMA:
        issues.append(f"contract_schema:{contract.get('schema')!r}")
    if route.get("scenario") != case.expected_scenario:
        issues.append(f"expected_scenario:{case.expected_scenario}:actual:{route.get('scenario')}")
    if route.get("tool") != case.expected_tool:
        issues.append(f"expected_tool:{case.expected_tool}:actual:{route.get('tool')}")
    if route.get("available") is not case.expected_available:
        issues.append(f"expected_available:{case.expected_available}:actual:{route.get('available')}")
    if case.expected_missing_params is not None:
        missing = tuple(route.get("missing_params") or [])
        if missing != case.expected_missing_params:
            issues.append(
                "expected_missing_params:"
                f"{list(case.expected_missing_params)}:actual:{list(missing)}"
            )

    if contract.get("route_first") is not True:
        issues.append("contract_route_first_not_true")
    if contract.get("primary_tool") != route.get("tool"):
        issues.append(
            f"contract_primary_tool:{contract.get('primary_tool')}:actual_tool:{route.get('tool')}"
        )
    chain = route.get("expected_tool_chain") or []
    if not isinstance(chain, list) or not chain or chain[0] != route.get("tool"):
        issues.append(f"expected_tool_chain_primary_mismatch:{chain!r}")
    if route.get("route_fingerprint") != contract.get("route_fingerprint"):
        issues.append("route_fingerprint_contract_mismatch")

    params = route.get("params") if isinstance(route.get("params"), dict) else {}
    if case.require_save_forced_false is True:
        if params.get("save") is not False:
            issues.append(f"params_save_not_forced_false:{params.get('save')!r}")
        if execution_policy.get("save_forced_false") is not True:
            issues.append("execution_policy_save_forced_false_not_true")
    elif case.require_save_forced_false is False:
        if execution_policy.get("save_forced_false") is not False:
            issues.append("execution_policy_save_forced_false_not_false")

    if case.require_compact_default_true is True:
        if params.get("compact") is not True:
            issues.append(f"params_compact_not_true:{params.get('compact')!r}")
        if execution_policy.get("compact_default_true") is not True:
            issues.append("execution_policy_compact_default_true_not_true")
    elif case.require_compact_default_true is False:
        if execution_policy.get("compact_default_true") is not False:
            issues.append("execution_policy_compact_default_true_not_false")

    if case.require_operator_policy:
        if execution_policy.get("meta_executable") is not False:
            issues.append("operator_meta_executable_not_false")
        if execution_policy.get("operator_review_requires_operator_surface") is not True:
            issues.append("operator_surface_flag_not_true")
    elif route.get("scenario") != "operator_review" and execution_policy.get("meta_executable") is not True:
        issues.append("public_route_meta_executable_not_true")

    guard_codes = {
        str(flag.get("code") or "")
        for flag in route.get("guard_flags") or []
        if isinstance(flag, dict)
    }
    for code in case.expected_guard_codes:
        if code not in guard_codes:
            issues.append(f"missing_guard_code:{code}")

    risk_codes = {
        str(flag.get("code") or "")
        for flag in route.get("risk_flags") or []
        if isinstance(flag, dict)
    }
    for code in case.expected_risk_codes:
        if code not in risk_codes:
            issues.append(f"missing_risk_code:{code}")

    guide_template = route.get("guide_prompt_template")
    if case.require_guide_prompt_template and not isinstance(guide_template, dict):
        issues.append("missing_guide_prompt_template")
    if isinstance(guide_template, dict):
        contract_template = contract.get("guide_prompt_template")
        if not isinstance(contract_template, dict):
            issues.append("missing_contract_guide_prompt_template")
        elif contract_template.get("expected_tool") != route.get("tool"):
            issues.append(
                "contract_guide_expected_tool_mismatch:"
                f"{contract_template.get('expected_tool')}:{route.get('tool')}"
            )

    return issues


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Query Route Contract Audit",
        "",
        f"- schema: `{payload['schema']}`",
        f"- generated_at: `{payload['generated_at']}`",
        f"- ok: `{str(payload['ok']).lower()}`",
        f"- status: `{payload['status']}`",
        f"- cases: `{payload['pass_count']} / {payload['case_count']}` passed",
        f"- status_update_allowed: `{str(payload['status_update_allowed']).lower()}`",
        f"- db_writes: `{str(payload['db_writes']).lower()}`",
        f"- approval_claim: `{str(payload['approval_claim']).lower()}`",
        "",
        "## Cases",
        "",
        "| status | name | scenario | tool | route fingerprint | issues |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in payload["rows"]:
        actual = row["actual"]
        issues = "<br>".join(row["issues"]) if row["issues"] else ""
        lines.append(
            "| {status} | `{name}` | `{scenario}` | `{tool}` | `{fingerprint}` | {issues} |".format(
                status=row["status"].upper(),
                name=row["name"],
                scenario=actual.get("scenario"),
                tool=actual.get("tool"),
                fingerprint=actual.get("route_fingerprint") or "",
                issues=issues,
            )
        )
    if payload["failure_summary"]:
        lines.extend(["", "## Failures", ""])
        for failure in payload["failure_summary"]:
            lines.append(f"- `{failure['name']}`: {', '.join(failure['issues'])}")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit NCS query-route contracts without writing DB or review statuses."
    )
    parser.add_argument(
        "--case",
        dest="cases",
        action="append",
        type=parse_case,
        help=(
            "Optional custom route case. Format: "
            "'query|expected_scenario|expected_tool' or "
            "'name|query|expected_scenario|expected_tool'."
        ),
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cases = args.cases if args.cases else default_cases()
    payload = audit_routes(cases)
    write_json(args.out, payload)
    write_markdown(args.markdown_out, payload)
    print(json.dumps({"ok": payload["ok"], "out": rel(args.out), "markdown_out": rel(args.markdown_out)}, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
