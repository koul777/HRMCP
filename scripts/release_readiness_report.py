from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


HUMAN_REVIEW_GATES = {
    "review_debt:candidate_definition_ratio",
    "review_debt:human_reviewed_concepts",
    "review_debt:human_reviewed_goal_links",
    "review_debt:human_reviewed_task_relations",
}


def _gate_by_name(quality_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(gate.get("name")): gate
        for gate in quality_report.get("gates", [])
        if isinstance(gate, dict)
    }


def build_release_readiness(
    quality_report: dict[str, Any],
    contract: dict[str, Any],
    *,
    min_trusted_scenarios: int = 10,
    min_qualification_coverage: float = 0.9,
) -> dict[str, Any]:
    gates = _gate_by_name(quality_report)
    summary = quality_report.get("summary") or {}
    contract_surface = contract.get("surface") or {}
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    fail_count = int(summary.get("fail_count") or 0)
    if fail_count:
        blockers.append(
            {
                "category": "engineering_hygiene",
                "name": "quality_gate_failures",
                "message": "Quality gates have fail-severity results.",
                "value": fail_count,
            }
        )

    public_tool_count = int(contract_surface.get("active_tool_count") or 0)
    if public_tool_count != 10:
        blockers.append(
            {
                "category": "mcp_contract",
                "name": "public_tool_count",
                "message": "Public MCP contract should expose the compact 10-tool surface.",
                "value": public_tool_count,
            }
        )

    operator_tool_count = int(contract_surface.get("operator_tool_count") or 0)
    if operator_tool_count:
        blockers.append(
            {
                "category": "mcp_contract",
                "name": "operator_tools_exposed_publicly",
                "message": "Operator tools should be hidden in the default public contract.",
                "value": operator_tool_count,
            }
        )

    for gate_name in sorted(HUMAN_REVIEW_GATES):
        gate = gates.get(gate_name)
        if gate and gate.get("status") != "pass":
            blockers.append(
                {
                    "category": "human_review",
                    "name": gate_name,
                    "message": gate.get("message"),
                    "value": gate.get("value"),
                    "threshold": gate.get("threshold"),
                }
            )

    qualification_gate = gates.get("qualification:collection_coverage")
    if qualification_gate:
        coverage = float(qualification_gate.get("value") or 0)
        if coverage < min_qualification_coverage:
            blockers.append(
                {
                    "category": "data_collection",
                    "name": "qualification:collection_coverage",
                    "message": "Qualification collection coverage is below the release target.",
                    "value": coverage,
                    "threshold": f">= {min_qualification_coverage}",
                    "details": qualification_gate.get("details") or {},
                }
            )

    trusted_gate = gates.get("transition_eval:trusted_scenarios")
    trusted_count = int((trusted_gate or {}).get("value") or 0)
    if trusted_count < min_trusted_scenarios:
        blockers.append(
            {
                "category": "evaluation",
                "name": "trusted_transition_scenarios",
                "message": "Trusted transition scenarios are too sparse for release-grade evaluation.",
                "value": trusted_count,
                "threshold": f">= {min_trusted_scenarios}",
            }
        )

    blocker_names = {str(blocker.get("name")) for blocker in blockers}
    for gate in quality_report.get("gates", []):
        gate_name = str(gate.get("name"))
        if (
            gate.get("status") == "warn"
            and gate_name not in HUMAN_REVIEW_GATES
            and gate_name not in blocker_names
        ):
            warnings.append(
                {
                    "name": gate_name,
                    "message": gate.get("message"),
                    "value": gate.get("value"),
                    "threshold": gate.get("threshold"),
                }
            )

    engineering_hygiene_ok = (
        fail_count == 0
        and public_tool_count == 10
        and operator_tool_count == 0
    )
    release_ready = not blockers
    return {
        "ok": True,
        "release_ready": release_ready,
        "engineering_hygiene_ok": engineering_hygiene_ok,
        "blocker_count": len(blockers),
        "warning_count": len(warnings),
        "blockers": blockers,
        "warnings": warnings,
        "inputs": {
            "quality_status": quality_report.get("status"),
            "quality_summary": summary,
            "contract_surface": contract_surface,
            "min_trusted_scenarios": min_trusted_scenarios,
            "min_qualification_coverage": min_qualification_coverage,
        },
    }


def write_markdown(report: dict[str, Any], out_path: Path) -> None:
    lines = [
        "# NCS MCP Release Readiness",
        "",
        f"- release_ready: {str(report.get('release_ready')).lower()}",
        f"- engineering_hygiene_ok: {str(report.get('engineering_hygiene_ok')).lower()}",
        f"- blocker_count: {report.get('blocker_count')}",
        f"- warning_count: {report.get('warning_count')}",
        "",
        "## Blockers",
        "",
    ]
    blockers = report.get("blockers") or []
    if not blockers:
        lines.append("- none")
    for blocker in blockers:
        lines.append(
            "- "
            + f"[{blocker.get('category')}] {blocker.get('name')}: "
            + f"{blocker.get('message')} "
            + f"(value={blocker.get('value')}, threshold={blocker.get('threshold')})"
        )
    lines.extend(["", "## Warnings", ""])
    warnings = report.get("warnings") or []
    if not warnings:
        lines.append("- none")
    for warning in warnings:
        lines.append(
            "- "
            + f"{warning.get('name')}: {warning.get('message')} "
            + f"(value={warning.get('value')}, threshold={warning.get('threshold')})"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- `engineering_hygiene_ok=true` means tests, contract shape, and public tool boundary can be green.",
            "- `release_ready=false` means data/review evidence is still insufficient for benchmark-grade release claims.",
        ]
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an NCS MCP release-readiness report.")
    parser.add_argument("--quality-report", required=True, type=Path)
    parser.add_argument("--contract", default=ROOT / "mcp" / "ncs-tool-contract.json", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--min-trusted-scenarios", type=int, default=10)
    parser.add_argument("--min-qualification-coverage", type=float, default=0.9)
    parser.add_argument("--fail-on-blockers", action="store_true")
    args = parser.parse_args(argv)

    quality_report = json.loads(args.quality_report.read_text(encoding="utf-8"))
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    report = build_release_readiness(
        quality_report,
        contract,
        min_trusted_scenarios=args.min_trusted_scenarios,
        min_qualification_coverage=args.min_qualification_coverage,
    )
    if args.markdown_out:
        report["markdown_path"] = str(args.markdown_out)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown_out:
        write_markdown(report, args.markdown_out)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.fail_on_blockers and report["blockers"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
