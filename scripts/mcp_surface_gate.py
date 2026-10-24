"""Release QA gate for the public MCP tool surface.

Rules written only in documents get skipped, so this gate checks the tool
surface in code: tool count, definition token budget, schema shapes that weak
tool-calling models and clients can handle, tool-chain integrity, and
read-only guarantees. Thresholds are constants below; changing one is a
contract change and needs a dated reason in docs/HARNESS_ENGINEERING.md.

Usage: python scripts/mcp_surface_gate.py [--out report.json]
Exit code 1 when any check fails. The script pins the release profile
(advanced and operator tools disabled) before importing the server.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import inspect
import json
import os
import re
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
for _path in (SRC, PROJECT_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

SCHEMA = "ncs_mcp_surface_gate_v1"
# The Korean law MCP settled at 10-12 public tools after starting at 89.
MAX_ACTIVE_TOOLS = 12
TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")  # OpenAI, Gemini and Ollama all accept this
MIN_DESCRIPTION_CHARS = 30  # too short and models cannot tell when to chain the tool
MAX_DESCRIPTION_CHARS = 600  # too long and every request pays for it
MAX_DEFINITION_CHARS = 9000  # 2026-10-24 baseline 5,544 chars for 7 tools
MAX_DEFINITION_TOKENS_EST = 4000
MAX_PROPERTIES = 16
MAX_REQUIRED = 3
MAX_SCHEMA_DEPTH = 4
FORBIDDEN_SCHEMA_KEYWORDS = {
    "$ref", "$defs", "definitions", "oneOf", "allOf", "not", "patternProperties",
    "if", "then", "else", "dependentSchemas", "dependentRequired",
}
CONCRETE_TYPES = {"string", "integer", "number", "boolean", "array", "object"}
# Free-form objects are hard for weak models; only these parameters may contain one.
OBJECT_PARAM_ALLOWLIST = {
    ("ncs_search", "classification_filter"),
    ("ncs_discover_tools", "classification_filter"),
    ("ncs_execute_tool", "params"),
}
WRITE_PARAM_NAMES = {"save", "apply", "write", "commit", "delete", "overwrite"}
PATH_PARAM_NAMES = {"db_path", "path", "file", "file_path", "out", "output"}
DYNAMIC_FORWARDERS = {"locals", "vars", "globals"}
MAX_NEXT_ACTIONS = 3
JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
    "array": (list,), "object": (dict,),
}


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: one token per non-ASCII char, four ASCII chars per token."""
    non_ascii = sum(1 for char in text if ord(char) > 127)
    return non_ascii + (len(text) - non_ascii + 3) // 4


def _check(check_id: str, ok: bool, measured: Any, threshold: Any, details: Any = None, *, warn: bool = False) -> dict[str, Any]:
    status = "pass" if ok else ("warn" if warn else "fail")
    return {"id": check_id, "status": status, "measured": measured, "threshold": threshold, "details": details or []}


def _schema_depth(node: Any, depth: int = 0) -> int:
    if isinstance(node, dict):
        return max([depth] + [_schema_depth(value, depth + 1) for value in node.values()])
    if isinstance(node, list):
        return max([depth] + [_schema_depth(value, depth) for value in node])
    return depth


def _walk(node: Any, path: str = ""):
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}/{index}")


def _is_nullable_pair(branches: Any) -> bool:
    """anyOf may only make one concretely typed schema nullable."""
    if not isinstance(branches, list) or len(branches) != 2:
        return False
    nulls = [branch for branch in branches if branch == {"type": "null"}]
    others = [branch for branch in branches if branch != {"type": "null"}]
    return len(nulls) == 1 and isinstance(others[0], dict) and others[0].get("type") in CONCRETE_TYPES


def _contains_object(node: Any) -> bool:
    return any(sub.get("type") == "object" or "additionalProperties" in sub or "properties" in sub
               for _, sub in _walk(node))


def schema_issues(tool_name: str, schema: dict[str, Any]) -> list[str]:
    """Schema shapes that weak tool-calling clients mishandle."""
    issues = []
    if schema.get("type") != "object":
        issues.append("top_level_not_object")
    properties = schema.get("properties") or {}
    if len(properties) > MAX_PROPERTIES:
        issues.append(f"properties>{MAX_PROPERTIES}:{len(properties)}")
    if len(schema.get("required") or []) > MAX_REQUIRED:
        issues.append(f"required>{MAX_REQUIRED}:{len(schema.get('required') or [])}")
    if _schema_depth(schema) > MAX_SCHEMA_DEPTH * 2:  # each schema level is two dict levels deep
        issues.append(f"depth>{MAX_SCHEMA_DEPTH}")
    for path, node in _walk(schema):
        if path.endswith("/properties"):
            continue  # keys of a properties map are parameter names, not keywords
        for keyword in sorted(FORBIDDEN_SCHEMA_KEYWORDS & set(node)):
            issues.append(f"forbidden:{keyword}@{path or '/'}")
        if "anyOf" in node and not _is_nullable_pair(node["anyOf"]):
            issues.append(f"anyOf_not_nullable_pair@{path or '/'}")
    for name, prop in properties.items():
        if _contains_object(prop) and (tool_name, name) not in OBJECT_PARAM_ALLOWLIST:
            issues.append(f"object_param_not_allowlisted:{name}")
    return issues


def tool_definitions(tools: list[Any]) -> list[dict[str, Any]]:
    definitions = []
    for tool in tools:
        data = tool.model_dump() if hasattr(tool, "model_dump") else dict(tool)
        definitions.append({"name": data.get("name"), "description": data.get("description") or "",
                            "inputSchema": data.get("inputSchema") or {}})
    return sorted(definitions, key=lambda item: item["name"])


def check_definitions(definitions: list[dict[str, Any]], release_tools: set[str]) -> list[dict[str, Any]]:
    """Every listed tool is checked; nothing is filtered out first."""
    names = [item["name"] for item in definitions]
    checks = [
        _check("tool_count", 1 <= len(names) <= MAX_ACTIVE_TOOLS, len(names), f"1..{MAX_ACTIVE_TOOLS}", names),
        _check("release_profile_matches_registry", set(names) == release_tools,
               {"unexpected": sorted(set(names) - release_tools), "missing": sorted(release_tools - set(names))},
               "live tools/list == tool_registry release profile"),
        _check("tool_name_format", all(TOOL_NAME_PATTERN.match(name or "") for name in names),
               [name for name in names if not TOOL_NAME_PATTERN.match(name or "")], TOOL_NAME_PATTERN.pattern),
    ]
    lengths = {item["name"]: len(item["description"].strip()) for item in definitions}
    bad_lengths = {name: size for name, size in lengths.items()
                   if not MIN_DESCRIPTION_CHARS <= size <= MAX_DESCRIPTION_CHARS}
    checks.append(_check("description_length", not bad_lengths, lengths,
                         f"{MIN_DESCRIPTION_CHARS}..{MAX_DESCRIPTION_CHARS} chars", bad_lengths))
    blob = json.dumps(definitions, ensure_ascii=False, separators=(",", ":"))
    checks.append(_check("definition_chars", len(blob) <= MAX_DEFINITION_CHARS, len(blob), MAX_DEFINITION_CHARS))
    tokens = estimate_tokens(blob)
    checks.append(_check("definition_tokens_est", tokens <= MAX_DEFINITION_TOKENS_EST, tokens, MAX_DEFINITION_TOKENS_EST))
    issues = {item["name"]: schema_issues(item["name"], item["inputSchema"]) for item in definitions}
    issues = {name: found for name, found in issues.items() if found}
    checks.append(_check("schema_shape", not issues, sum(len(found) for found in issues.values()),
                         "nullable anyOf of a concrete type only; no $ref/oneOf/allOf; allowlisted objects", issues))
    undocumented = sorted(f"{item['name']}.{name}" for item in definitions
                          for name, prop in (item["inputSchema"].get("properties") or {}).items()
                          if not prop.get("description"))
    total = sum(len(item["inputSchema"].get("properties") or {}) for item in definitions)
    # Measured only: per-parameter descriptions cost tokens; the small-model eval decides if they pay off.
    checks.append(_check("parameter_descriptions", not undocumented, f"{total - len(undocumented)}/{total}",
                         "measured; warn only", undocumented, warn=True))
    return checks


def function_source(func: Callable[..., Any]) -> str:
    """Source of the registered callable itself, unwrapping decorators."""
    return textwrap.dedent(inspect.getsource(inspect.unwrap(func)))


def _function_node(source: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    tree = ast.parse(source)
    return next((node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))), None)


def _parameter_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = node.args
    return {arg.arg for arg in args.posonlyargs + args.args + args.kwonlyargs}


def check_read_only(sources: dict[str, str | None], definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A public tool may accept a legacy write flag but must never read or forward it."""
    used_write_flags, path_params, dynamic, missing = [], [], [], []
    for item in definitions:
        name = item["name"]
        node = _function_node(sources[name]) if sources.get(name) else None
        if node is None:
            missing.append(name)
            continue
        params = _parameter_names(node)
        if node.args.vararg or node.args.kwarg:
            dynamic.append(f"{name}:*args/**kwargs")
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id in DYNAMIC_FORWARDERS:
                dynamic.append(f"{name}:{child.func.id}()@line{child.lineno}")
        for flag in sorted(params & WRITE_PARAM_NAMES):
            loads = [child for child in ast.walk(node)
                     if isinstance(child, ast.Name) and child.id == flag and isinstance(child.ctx, ast.Load)]
            if loads:
                used_write_flags.append(f"{name}.{flag}@line{loads[0].lineno}")
        path_params.extend(f"{name}.{param}" for param in sorted(params & PATH_PARAM_NAMES))
    return [
        _check("public_tools_found_in_source", not missing, missing, "every listed tool resolves to its function source"),
        _check("write_flags_never_read", not used_write_flags, used_write_flags,
               f"parameters {sorted(WRITE_PARAM_NAMES)} unused in public tool bodies"),
        _check("no_dynamic_argument_forwarding", not dynamic, dynamic,
               "no *args/**kwargs or locals()/vars()/globals() that could forward a write flag"),
        _check("no_path_parameters", not path_params, path_params, "public tools take no file or DB paths"),
    ]


def check_meta_execution(server: Any, registry: Any, executable: set[str]) -> list[dict[str, Any]]:
    """ncs_execute_tool dispatches the named handler and forces save=False, checked by calling it with a stub."""
    handlers = getattr(server, "NCS_EXECUTABLE_TOOL_HANDLERS", {})
    mismatched = sorted(f"{name}->{inspect.unwrap(handler).__name__}" for name, handler in handlers.items()
                        if inspect.unwrap(handler).__name__ != name)
    unknown = sorted(set(handlers) - set(registry.NCS_EXECUTABLE_TOOL_NAMES))
    exposed_write = sorted(set(registry.NCS_EXECUTABLE_TOOL_NAMES) & (registry.OPERATOR_MCP_TOOLS | registry.LEGACY_MCP_TOOLS))
    with_save = sorted(name for name in executable if name in handlers
                       and "save" in inspect.signature(inspect.unwrap(handlers[name])).parameters)
    not_forced = sorted(set(with_save) - registry.NCS_META_READ_ONLY_SAVE_FORCED_TOOLS)
    forwarded = {}
    for name in with_save:
        captured: dict[str, Any] = {}

        def stub(*args: Any, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"ok": True}

        stub.__signature__ = inspect.signature(inspect.unwrap(handlers[name]))  # type: ignore[attr-defined]
        with patch.dict(handlers, {name: stub}), patch.object(server, "open_db", side_effect=RuntimeError("no db")):
            try:
                server.ncs_execute_tool(name, {"save": True})
            except Exception as exc:  # the stub must be reached without the DB
                captured["_error"] = type(exc).__name__
        forwarded[name] = captured.get("save", captured.get("_error", "not_dispatched"))
    unsafe = {name: value for name, value in forwarded.items() if value is not False}
    return [
        _check("meta_execute_excludes_operator_and_legacy", not exposed_write and not unknown,
               {"operator_or_legacy": exposed_write, "unregistered_handlers": unknown}, "empty"),
        _check("meta_execute_handler_identity", not mismatched, mismatched, "each tool name dispatches its own function"),
        _check("meta_execute_forces_save_false", not not_forced and not unsafe and bool(with_save) == bool(forwarded),
               forwarded, "save=True from the caller reaches the handler as False", not_forced),
    ]


def _route_queries() -> list[tuple[str, str]]:
    from audit_query_route_contracts import default_cases
    from ncs_mcp.query_router import ROUTE_PATTERNS

    queries = [(case.name, case.query) for case in default_cases()]
    queries += [(f"pattern:{pattern.scenario}:{signal}", signal) for pattern in ROUTE_PATTERNS for signal in pattern.signals[:1]]
    return queries


def check_routes(listed: set[str], executable: set[str]) -> list[dict[str, Any]]:
    """Routes built the way ncs_discover_tools builds them must only send models to callable tools."""
    from ncs_mcp import tool_registry as registry
    from ncs_mcp.query_router import ROUTE_PATTERNS, route_ncs_query

    supported = set(registry.ALL_SUPPORTED_MCP_TOOLS)
    unknown = sorted({f"{pattern.scenario}:{tool}" for pattern in ROUTE_PATTERNS
                      for tool in (pattern.tool, *pattern.pipeline) if tool not in supported})
    callable_tools = listed | executable
    uncallable_primary, uncallable_chain = [], []
    for name, query in _route_queries():
        route = route_ncs_query(query, available_tool_names=listed)
        if route.get("available") and route.get("tool") not in callable_tools:
            uncallable_primary.append(f"{name}:{route.get('tool')}")
        uncallable_chain.extend(f"{name}:{tool}" for tool in (route.get("expected_tool_chain") or [])[1:]
                                if tool not in callable_tools)
    return [
        _check("route_tools_exist", not unknown, unknown, "route and pipeline tools are registered"),
        _check("route_primary_tool_callable", not uncallable_primary, uncallable_primary,
               "an available route points to a listed or meta-executable tool"),
        _check("route_chain_callable", not uncallable_chain, sorted(set(uncallable_chain)),
               "follow-up tools in expected_tool_chain are callable on this surface"),
    ]


def _value_matches(schema: dict[str, Any], value: Any) -> bool:
    branches = schema.get("anyOf") if isinstance(schema.get("anyOf"), list) else [schema]
    for branch in branches:
        kind = branch.get("type")
        if kind == "null" and value is None:
            return True
        if kind in JSON_TYPES and isinstance(value, JSON_TYPES[kind]) and not (kind != "boolean" and isinstance(value, bool)):
            return True
    return False


NEXT_ACTION_FIXTURES = [
    ("search hits", "ncs_search", {"query": "인사"}, {"ok": True, "results": [
        {"type": "unit", "id": "0202020101_23v3", "text": "인사기획"},
        {"type": "criteria", "id": 1, "path": {"unit_code": "0202020102_23v3"}}]},
     ["ncs_unit_detail", "ncs_unit_detail", "recommend_training_for_task"]),
    ("search not found", "ncs_search", {"query": "없는말"}, {"ok": False, "error": {"code": "NOT_FOUND"}},
     ["ncs_discover_tools"]),
    ("filtered not found", "ncs_search", {"query": "인사", "classification_filter": {"major_code": "20"}},
     {"ok": False, "error": {"code": "NOT_FOUND"}, "classification_filter": {"major_code": "20"}},
     ["ncs_discover_tools"]),
    ("scope error", "ncs_search", {"query": "인사"}, {"ok": False, "error": {"code": "route_context_required"}}, []),
    ("unit detail", "ncs_unit_detail", {"unit_code": "0202020101_23v3"}, {"ok": True},
     ["ncs_unit_detail", "recommend_training_for_task", "ncs_analysis"]),
    ("training search", "ncs_training", {"query": "인사"},
     {"ok": True, "training_courses": [{"training_course": {"training_course_id": 7}, "link_counts": {}}]},
     ["ncs_training"]),
    ("training detail", "ncs_training", {"training_course_id": 7}, {"ok": True, "content": []}, []),
    ("analysis with unit", "ncs_analysis", {"mode": "career_path", "unit_code": "0202020101_23v3"}, {"ok": True},
     ["ncs_unit_detail"]),
]


def check_next_actions(builder: Callable[..., list[dict[str, Any]]] | None,
                       definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Next-step hints must be complete, valid calls to listed tools and keep hard scopes."""
    if builder is None:
        return [_check("next_actions_integrity", False, "missing", "server._public_next_actions exists")]
    schemas = {item["name"]: item["inputSchema"] for item in definitions}
    problems = []
    for label, tool, arguments, result, expected in NEXT_ACTION_FIXTURES:
        actions = builder(tool, arguments, result)
        if [action.get("tool") for action in actions] != expected:
            problems.append(f"{label}:expected {expected} got {[action.get('tool') for action in actions]}")
        if len(actions) > MAX_NEXT_ACTIONS:
            problems.append(f"{label}:more_than_{MAX_NEXT_ACTIONS}")
        for action in actions:
            target, call_args = action.get("tool"), action.get("arguments") or {}
            if target not in schemas:
                problems.append(f"{label}->{target}:not_listed")
                continue
            properties = schemas[target].get("properties") or {}
            for arg in sorted(set(schemas[target].get("required") or []) - set(call_args)):
                problems.append(f"{label}->{target}:missing_required:{arg}")
            for arg, value in call_args.items():
                if arg not in properties:
                    problems.append(f"{label}->{target}:unknown_arg:{arg}")
                elif not _value_matches(properties[arg], value):
                    problems.append(f"{label}->{target}:bad_type:{arg}")
            if not str(action.get("purpose") or "").strip():
                problems.append(f"{label}->{target}:no_purpose")
        if "classification_filter" in arguments and actions:
            if any(action.get("arguments", {}).get("classification_filter") != arguments["classification_filter"]
                   for action in actions if action.get("tool") in {"ncs_discover_tools", "ncs_search"}):
                problems.append(f"{label}:retry_drops_hard_scope")
    return [_check("next_actions_integrity", not problems, len(NEXT_ACTION_FIXTURES),
                   f"expected tools per fixture, required args and types, hard scope kept, <= {MAX_NEXT_ACTIONS}",
                   problems)]


def run_gate() -> dict[str, Any]:
    from ncs_mcp import server, tool_registry as registry

    settings = server.load_settings()
    release_tools = registry.mcp_tools_for_mode(advanced_tools_enabled=False)
    executable = registry.executable_tool_names_for_mode(advanced_tools_enabled=False)
    definitions = tool_definitions(asyncio.run(server.mcp.list_tools()))
    listed = {item["name"] for item in definitions}
    profile_flags = {"advanced_tools_enabled": bool(getattr(settings, "advanced_tools_enabled", False)),
                     "operator_tools_enabled": bool(getattr(settings, "operator_tools_enabled", False))}
    checks = [_check("release_profile_settings", not any(profile_flags.values()), profile_flags,
                     "advanced and operator tools disabled")]
    checks += check_definitions(definitions, release_tools)
    sources = {}
    for name in listed:
        func = getattr(server, name, None)
        try:
            sources[name] = function_source(func) if callable(func) else None
        except (OSError, TypeError):
            sources[name] = None
    checks += check_read_only(sources, definitions)
    checks += check_meta_execution(server, registry, executable)
    checks += check_routes(listed, executable)
    checks += check_next_actions(getattr(server, "_public_next_actions", None), definitions)
    canonical = json.dumps(definitions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    failed = [check["id"] for check in checks if check["status"] == "fail"]
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": not failed,
        "failed_checks": failed,
        "warned_checks": [check["id"] for check in checks if check["status"] == "warn"],
        "release_profile": "advanced_disabled_contract",
        "tool_definitions_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "checks": checks,
        "limitations": [
            "static and stubbed checks only; whether a small model actually chains the tools is measured by "
            "scripts/eval_small_model_tool_chaining.py",
            "token counts are estimates (non-ASCII chars count as one token each)",
            "next-step hints are checked on fixtures, not on every live response",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    os.environ["NCS_MCP_ENABLE_ADVANCED_TOOLS"] = "0"
    os.environ["NCS_MCP_ENABLE_OPERATOR_TOOLS"] = "0"
    report = run_gate()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    summary = {key: report[key] for key in ("ok", "failed_checks", "warned_checks", "tool_definitions_sha256")}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
