from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp import tool_registry
from ncs_mcp.query_router import ROUTE_CONTRACT_SCHEMA, ROUTE_FINGERPRINT_VERSION, ROUTE_PATTERNS


def build_contract(
    *,
    include_operator_tools: bool = False,
    include_advanced_tools: bool = False,
) -> dict[str, Any]:
    category_by_tool: dict[str, list[str]] = {}
    for category, tool_names in tool_registry.NCS_TOOL_CATEGORIES.items():
        for tool_name in tool_names:
            category_by_tool.setdefault(tool_name, []).append(category)

    active_tools = tool_registry.mcp_tools_for_mode(
        operator_tools_enabled=include_operator_tools,
        advanced_tools_enabled=include_advanced_tools,
    )
    tools = []
    for tool_name in sorted(active_tools):
        profile = tool_registry.NCS_TOOL_PROFILES.get(tool_name, {})
        role = "operator" if tool_name in tool_registry.OPERATOR_MCP_TOOLS else "user"
        executable = tool_name in tool_registry.NCS_EXECUTABLE_TOOL_NAMES
        tools.append(
            {
                "name": tool_name,
                "role": role,
                "categories": sorted(category_by_tool.get(tool_name, [])),
                "description": profile.get("description", ""),
                "aliases": profile.get("aliases", []),
                "executable_via_meta": executable,
                "meta_save_forced_false": tool_name in tool_registry.NCS_META_READ_ONLY_SAVE_FORCED_TOOLS,
                "meta_compact_default": tool_name in tool_registry.NCS_META_COMPACT_DEFAULT_TOOLS,
            }
        )

    return {
        "name": "ncs-mcp",
        "version": "0.1.0",
        "surface": {
            "mode": "admin" if include_operator_tools else "public",
            "active_tool_count": len(active_tools),
            "user_tool_count": len(tool_registry.USER_MCP_TOOLS & active_tools),
            "operator_tool_count": len(tool_registry.OPERATOR_MCP_TOOLS & active_tools),
            "operator_tool_count_available": len(tool_registry.OPERATOR_MCP_TOOLS),
            "advanced_tool_count": len(tool_registry.ADVANCED_MCP_TOOLS & active_tools),
            "advanced_tool_count_available": len(tool_registry.ADVANCED_MCP_TOOLS),
            "legacy_hidden_tool_count": len(tool_registry.LEGACY_MCP_TOOLS),
        },
        "policy": {
            "active_scope": "NCS-centered training recommendation",
            "legacy_scope": "SQF and NCS learning modules are hidden reference paths.",
            "meta_execution": "Only read-only user tools are executable through ncs_execute_tool.",
            "operator_tools": "Hidden by default; enable with NCS_MCP_ENABLE_OPERATOR_TOOLS=1 before server start.",
            "advanced_tools": (
                "Ontology / education-integration / transition tools are hidden by default "
                "for the public release; enable with NCS_MCP_ENABLE_ADVANCED_TOOLS=1 before "
                "server start once they are stabilized."
            ),
            "recommendation_meta_calls_force_save_false": True,
            "recommendation_meta_calls_default_compact_true": True,
            "query_routing": (
                "ncs_discover_tools returns a Law MCP-style query_route with scenario, "
                "tool, params, required_params, missing_params, pipeline, risk_flags, "
                "guard_flags, and a stable route_fingerprint."
            ),
            "route_integrity": (
                "ncs_execute_tool accepts _route_query and optional _route_fingerprint, "
                "recomputes the route, rejects mismatched fingerprints, and only executes "
                "the routed primary tool or expected tool-chain tools."
            ),
        },
        "query_router": {
            "schema": ROUTE_CONTRACT_SCHEMA,
            "fingerprint_version": ROUTE_FINGERPRINT_VERSION,
            "scenario_count": len(ROUTE_PATTERNS),
            "scenarios": [
                {
                    "scenario": pattern.scenario,
                    "tool": pattern.tool,
                    "required_params": list(pattern.required_params),
                    "pipeline": list(pattern.pipeline),
                    "requires_operator_surface": pattern.tool in tool_registry.OPERATOR_MCP_TOOLS,
                    "public_executable": pattern.tool in active_tools,
                    "expected_tool_chain": [
                        pattern.tool,
                        *[tool_name for tool_name in pattern.pipeline if tool_name != pattern.tool],
                    ],
                }
                for pattern in ROUTE_PATTERNS
            ],
        },
        "tools": tools,
        "operator_tools_available": sorted(tool_registry.OPERATOR_MCP_TOOLS),
        "advanced_tools_available": sorted(tool_registry.ADVANCED_MCP_TOOLS),
        "hidden_legacy_tools": sorted(tool_registry.LEGACY_MCP_TOOLS),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export or check the public NCS MCP tool contract.")
    parser.add_argument("--out", default="mcp/ncs-tool-contract.json", help="Output JSON path.")
    parser.add_argument("--check", action="store_true", help="Fail if the existing output file differs.")
    parser.add_argument(
        "--include-operator-tools",
        action="store_true",
        help="Export the admin/operator surface instead of the default public surface.",
    )
    parser.add_argument(
        "--include-advanced-tools",
        action="store_true",
        help="Include advanced ontology/education-integration/transition tools in the export.",
    )
    args = parser.parse_args(argv)

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    contract_text = (
        json.dumps(
            build_contract(
                include_operator_tools=args.include_operator_tools,
                include_advanced_tools=args.include_advanced_tools,
            ),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    if args.check:
        if not out_path.exists():
            print(f"contract file missing: {out_path}", file=sys.stderr)
            return 1
        existing = out_path.read_text(encoding="utf-8")
        if existing != contract_text:
            print(f"contract file is stale: {out_path}", file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "checked": str(out_path)}, ensure_ascii=False, indent=2))
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(contract_text, encoding="utf-8")
    print(json.dumps({"ok": True, "out": str(out_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
