"""Measure local-model tool chaining against answers sampled from SQLite."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
import time
from typing import Any, Awaitable, Callable, Sequence
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
CASES_SCHEMA = "ncs_small_model_chaining_cases_v1"
EVAL_SCHEMA = "ncs_small_model_chaining_eval_v1"
SEED = 20261024
TOOL_RESULT_LIMIT = 6000
# Without a cap a looping model generates until the client times out and the
# server keeps serving the abandoned request; Ollama's default 4,096-token
# context silently drops the start of longer prompts, including tool definitions.
MODEL_OPTIONS = {"temperature": 0, "seed": SEED, "num_predict": 2048, "num_ctx": 12288}
SYSTEM_PROMPT = "너는 NCS(국가직무능력표준) 도우미다. 제공된 도구로 확인한 내용만 근거로 한국어로 답한다."
NO_DATA = re.compile(r"찾을수없|없습니다|존재하지않|확인되지않|정보가없")
HttpFunction = Callable[[str, dict[str, Any] | None], dict[str, Any]]
ToolCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]


def canonical_json(value: Any) -> str:
    """Serialize reproducibly for fingerprints and JSONL records."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_cases(db_path: Path, per_major: int = 1, seed: int = SEED) -> dict[str, Any]:
    """Sample sorted, unambiguous units in each major without writing to SQLite."""
    if per_major < 1:
        raise ValueError("per_major must be positive")
    db_path = db_path.resolve(strict=True)
    stat = db_path.stat()
    rng = random.Random(seed)
    cases: list[dict[str, Any]] = []
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("classifications", "competency_units", "competency_elements")
        }
        units = conn.execute(
            """
            WITH unique_names AS (
                SELECT unit_name_raw FROM competency_units
                GROUP BY unit_name_raw HAVING COUNT(*) = 1
            )
            SELECT u.unit_code, u.unit_name_raw, c.major_code, c.major_name, c.sub_name
            FROM competency_units AS u
            JOIN classifications AS c ON c.classification_id = u.classification_id
            JOIN unique_names AS n ON n.unit_name_raw = u.unit_name_raw
            WHERE TRIM(u.unit_name_raw) <> '' AND TRIM(c.major_code) <> ''
              AND TRIM(c.major_name) <> '' AND TRIM(c.sub_name) <> ''
              AND EXISTS (
                  SELECT 1 FROM competency_elements AS e WHERE e.unit_code = u.unit_code
              )
              AND NOT EXISTS (
                  SELECT 1 FROM competency_elements AS e WHERE e.unit_code = u.unit_code
                    AND (e.element_name_raw IS NULL OR TRIM(e.element_name_raw) = '')
              )
            ORDER BY c.major_code, u.unit_code
            """
        ).fetchall()
        groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for unit in units:
            groups[unit["major_code"]].append(unit)
        for major in sorted(groups):
            selected = rng.sample(groups[major], min(per_major, len(groups[major])))
            for unit in sorted(selected, key=lambda row: row["unit_code"]):
                elements = conn.execute(
                    "SELECT element_name_raw FROM competency_elements WHERE unit_code = ? "
                    "ORDER BY element_no, element_id",
                    (unit["unit_code"],),
                ).fetchall()
                common = {"unit_code": unit["unit_code"], "major_code": major}
                name = unit["unit_name_raw"]
                cases.extend([
                    {
                        **common,
                        "id": f"elements-{unit['unit_code']}",
                        "template": "elements",
                        "question": f"'{name}' 능력단위의 능력단위요소를 모두 알려줘.",
                        "expected_terms": list(dict.fromkeys(row[0] for row in elements)),
                        "pass_rule": "at_least_half",
                    },
                    {
                        **common,
                        "id": f"classification-{unit['unit_code']}",
                        "template": "classification",
                        "question": f"'{name}' 능력단위는 NCS 분류체계에서 어느 대분류와 세분류에 속해?",
                        "expected_terms": [unit["major_name"], unit["sub_name"]],
                        "pass_rule": "all",
                    },
                ])
    if not cases:
        raise ValueError("No eligible units with named elements and classifications")
    return {
        "schema": CASES_SCHEMA,
        "seed": seed,
        "per_major": per_major,
        "db": {
            "path": str(db_path),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        },
        "table_row_counts": counts,
        "generated_at": utc_now(),
        "cases": cases,
        "cases_sha256": sha256_json(cases),
    }


def validate_cases(document: dict[str, Any]) -> None:
    """Reject damaged case files before contacting the model."""
    if document.get("schema") != CASES_SCHEMA:
        raise ValueError("Unsupported case schema")
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("The case file must contain cases")
    if document.get("cases_sha256") != sha256_json(cases):
        raise ValueError("Case fingerprint does not match the case list")
    seen: set[str] = set()
    for case in cases:
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError("Case IDs must be unique nonempty strings")
        seen.add(case_id)
        terms = case.get("expected_terms")
        if not isinstance(terms, list) or not terms or any(
            not isinstance(term, str) or not term.strip() for term in terms
        ):
            raise ValueError("Expected terms must be nonempty strings")
        if case.get("pass_rule") not in {"all", "at_least_half"}:
            raise ValueError("Unknown case pass rule")
        if not case.get("question") or not case.get("template"):
            raise ValueError("Each case needs a question and template")


def normalize_text(text: str) -> str:
    """Ignore whitespace and unify the two Korean middle-dot variants."""
    return re.sub(r"\s+", "", text).replace("ㆍ", "·")


def score_answer(case: dict[str, Any], answer: str) -> dict[str, Any]:
    normalized = normalize_text(answer)
    terms = case["expected_terms"]
    matched = [term for term in terms if normalize_text(term) in normalized]
    if case["pass_rule"] == "all":
        required = len(terms)
    elif case["pass_rule"] == "at_least_half":
        required = math.ceil(len(terms) / 2)
    else:
        raise ValueError("Unknown case pass rule")
    passed = bool(terms) and len(matched) >= required
    return {
        "matched_expected_terms": matched,
        "passed": passed,
        "false_no_data": not passed and bool(NO_DATA.search(normalized)),
    }


def ollama_tools(tools: Sequence[Any]) -> list[dict[str, Any]]:
    """Keep the public MCP descriptions and input schemas unchanged."""
    return [
        {"type": "function", "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        }}
        for tool in tools
    ]


def tool_result_text(result: Any) -> str:
    """Join only TextContent, never the separate structured output copy."""
    # MCP 1.28 returns a list, a (content, structured) tuple, or CallToolResult.
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        result = result[0]
    if hasattr(result, "content"):
        result = result.content
    elif isinstance(result, dict):
        result = result["content"]
    texts = []
    for block in result:
        kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if kind == "text":
            texts.append(block["text"] if isinstance(block, dict) else block.text)
    return "\n".join(texts)


def http_json(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Use urllib for both version discovery and nonstreaming chat."""
    request = Request(
        url,
        data=None if payload is None else canonical_json(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urlopen(request, timeout=300 if payload is not None else 10) as response:
        value = json.load(response)
    if not isinstance(value, dict) or "error" in value:
        raise ValueError("Invalid Ollama response")
    return value


async def evaluate_case(
    case: dict[str, Any],
    tools: list[dict[str, Any]],
    *,
    tool_caller: ToolCaller,
    http_fn: HttpFunction = http_json,
    model: str = "qwen3:4b",
    ollama_url: str = "http://127.0.0.1:11434",
    max_turns: int = 6,
) -> dict[str, Any]:
    """Run one bounded chat; dependencies are injectable for offline tests."""
    if max_turns < 1:
        raise ValueError("max_turns must be positive")
    started = time.perf_counter()
    public_names = {tool["function"]["name"] for tool in tools}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": case["question"]},
    ]
    names: list[str] = []
    traces: list[dict[str, Any]] = []
    answer, stop_reason, error = "", "max_turns", None
    prompt_tokens = output_tokens = turns = max_prompt_tokens = length_stops = 0
    try:
        for turns in range(1, max_turns + 1):
            reply = http_fn(ollama_url.rstrip("/") + "/api/chat", {
                "model": model,
                "messages": messages,
                "tools": tools,
                "stream": False,
                "think": False,
                "options": MODEL_OPTIONS,
            })
            prompt_tokens += int(reply.get("prompt_eval_count") or 0)
            max_prompt_tokens = max(max_prompt_tokens, int(reply.get("prompt_eval_count") or 0))
            length_stops += reply.get("done_reason") == "length"
            output_tokens += int(reply.get("eval_count") or 0)
            message = reply["message"]
            content = message.get("content") or ""
            calls = message.get("tool_calls") or []
            if not isinstance(content, str) or not isinstance(calls, list):
                raise ValueError("Invalid assistant message")
            if not calls:
                answer, stop_reason = content, "answered"
                break
            messages.append({"role": "assistant", "content": content, "tool_calls": calls})
            for call in calls:
                function = call.get("function", {})
                name = function.get("name", "")
                if not isinstance(name, str):
                    name = ""
                names.append(name)
                tool_error = None
                if name not in public_names:
                    tool_error = "unknown_public_tool"
                    text = "Tool error: requested tool is not in the public tool list."
                else:
                    try:
                        arguments = function.get("arguments", {})
                        if isinstance(arguments, str):
                            arguments = json.loads(arguments)
                        if not isinstance(arguments, dict):
                            raise ValueError("Tool arguments must be an object")
                        text = tool_result_text(await tool_caller(name, arguments))
                    except Exception as exc:
                        # Exception messages may contain configuration or credentials.
                        tool_error = type(exc).__name__
                        text = f"Tool error: {tool_error}"
                original_chars = len(text)
                text = text[:TOOL_RESULT_LIMIT]
                traces.append({
                    "name": name,
                    "original_chars": original_chars,
                    "returned_chars": len(text),
                    "truncated": original_chars > TOOL_RESULT_LIMIT,
                    "error": tool_error,
                })
                messages.append({"role": "tool", "content": text, "tool_name": name})
    except Exception as exc:
        stop_reason, error = "error", type(exc).__name__
    return {
        "id": case["id"],
        "template": case["template"],
        "final_answer": answer,
        **score_answer(case, answer),
        "tool_calls": names,
        "ncs_unit_detail_called": "ncs_unit_detail" in names,
        "tool_results": traces,
        "turns": turns,
        "stop_reason": stop_reason,
        "error": error,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "prompt_eval_count": prompt_tokens,
        "eval_count": output_tokens,
        # A request near num_ctx means Ollama may have dropped the start of the prompt.
        "max_request_prompt_tokens": max_prompt_tokens,
        "length_stopped_replies": length_stops,
    }


def read_git_head(root: Path = ROOT) -> str | None:
    """Resolve loose, packed, detached, and worktree HEADs without subprocesses."""
    try:
        git_dir = root / ".git"
        if git_dir.is_file():
            git_dir = (root / git_dir.read_text(encoding="utf-8").strip().split(": ", 1)[1]).resolve()
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head
        ref = head[5:]
        common = git_dir / "commondir"
        directories = [git_dir]
        if common.exists():
            directories.append((git_dir / common.read_text(encoding="utf-8").strip()).resolve())
        for directory in directories:
            if (directory / ref).exists():
                return (directory / ref).read_text(encoding="utf-8").strip()
            packed = directory / "packed-refs"
            if packed.exists():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == ref:
                        return parts[0]
    except (OSError, IndexError):
        pass
    return None


def wilson_interval(passed: int, total: int) -> list[float | None]:
    """Return the two-sided Wilson 95 percent interval."""
    if not total:
        return [None, None]
    z = 1.959963984540054
    rate = passed / total
    denominator = 1 + z * z / total
    center = (rate + z * z / (2 * total)) / denominator
    half = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total)) / denominator
    return [max(0.0, center - half), min(1.0, center + half)]


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    passed = sum(row["passed"] for row in results)
    no_data = sum(row["false_no_data"] for row in results)
    templates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in results:
        templates[row["template"]].append(row)
    sequences = Counter(tuple(row["tool_calls"]) for row in results)
    return {
        "counts": {
            "evaluated": total, "passed": passed, "failed": total - passed,
            "false_no_data": no_data,
            "errors": sum(row["stop_reason"] == "error" for row in results),
            "max_turns": sum(row["stop_reason"] == "max_turns" for row in results),
        },
        "pass_rate": passed / total if total else None,
        "pass_rate_wilson_95": wilson_interval(passed, total),
        "false_no_data_rate": no_data / total if total else None,
        "per_template": {
            name: {"count": len(rows), "passed": sum(row["passed"] for row in rows),
                   "pass_rate": sum(row["passed"] for row in rows) / len(rows)}
            for name, rows in sorted(templates.items())
        },
        "mean_tool_calls": sum(len(row["tool_calls"]) for row in results) / total if total else None,
        "top_tool_call_sequences": [
            {"tools": list(sequence), "count": count}
            for sequence, count in sorted(sequences.items(), key=lambda pair: (-pair[1], pair[0]))[:10]
        ],
        "totals": {
            "prompt_eval_count": sum(row["prompt_eval_count"] for row in results),
            "eval_count": sum(row["eval_count"] for row in results),
            "elapsed_seconds": round(sum(row["elapsed_seconds"] for row in results), 6),
            "length_stopped_replies": sum(row.get("length_stopped_replies", 0) for row in results),
            "max_request_prompt_tokens": max((row.get("max_request_prompt_tokens", 0) for row in results), default=0),
        },
    }


def load_completed(path: Path, fingerprint: str, case_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Fail closed on mixed runs or damaged records instead of biasing the KPI."""
    completed: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("run_fingerprint") != fingerprint:
                raise ValueError("Resume configuration changed; use another output or --fresh")
            case_id = row["id"]
            if case_id not in case_ids or case_id in completed:
                raise ValueError("Unexpected or duplicate case ID in resume log")
            completed[case_id] = row
    return completed


async def run_evaluation(
    document: dict[str, Any],
    cases_path: Path,
    out: Path,
    tools: list[dict[str, Any]],
    *,
    tool_caller: ToolCaller,
    http_fn: HttpFunction = http_json,
    model: str = "qwen3:4b",
    ollama_url: str = "http://127.0.0.1:11434",
    max_turns: int = 6,
    limit: int | None = None,
    fresh: bool = False,
    runtime_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append durable case records and aggregate the selected prefix on resume."""
    validate_cases(document)
    if max_turns < 1 or (limit is not None and limit < 1):
        raise ValueError("max_turns and limit must be positive")
    if not tools:
        raise ValueError("No public tools available")
    log_path = Path(str(out) + ".cases.jsonl")
    inputs = {cases_path.resolve(), Path(document["db"]["path"]).resolve()}
    if out.resolve() in inputs or log_path.resolve() in inputs:
        raise ValueError("Output paths must not overwrite input files")
    version = http_fn(ollama_url.rstrip("/") + "/api/version", None)["version"]
    config = {
        "model": model,
        "ollama_url": ollama_url.rstrip("/"),
        "ollama_version": version,
        "cases_sha256": document["cases_sha256"],
        "tool_definitions_sha256": sha256_json(tools),
        "git_head": read_git_head(),
        "max_turns": max_turns,
        "system_prompt": SYSTEM_PROMPT,
        "options": MODEL_OPTIONS,
        "think": False,
        "tool_result_limit": TOOL_RESULT_LIMIT,
        "db": document["db"],
        # Which source tree served the tools; the git head alone cannot tell A/B arms apart.
        "runtime": runtime_identity or {},
    }
    fingerprint = sha256_json(config)
    cases = document["cases"][:limit]
    completed = {} if fresh else load_completed(
        log_path, fingerprint, {case["id"] for case in document["cases"]}
    )
    resumed = sum(case["id"] in completed for case in cases)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # A complete final record without a newline is still safe to resume.
    separator = False
    if not fresh and log_path.exists() and log_path.stat().st_size:
        with log_path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            separator = handle.read(1) != b"\n"
    with log_path.open("w" if fresh else "a", encoding="utf-8") as handle:
        for case in cases:
            if case["id"] in completed:
                continue
            result = await evaluate_case(
                case, tools, tool_caller=tool_caller, http_fn=http_fn,
                model=model, ollama_url=ollama_url, max_turns=max_turns,
            )
            result["run_fingerprint"] = fingerprint
            handle.write(("\n" if separator else "") + canonical_json(result) + "\n")
            handle.flush()
            separator = False
            completed[case["id"]] = result
    summary = {
        "schema": EVAL_SCHEMA,
        "generated_at": utc_now(),
        **config,
        "case_file_path": str(cases_path.resolve()),
        "case_results_path": str(log_path.resolve()),
        "run_fingerprint": fingerprint,
        **summarize([completed[case["id"]] for case in cases]),
        "limitations": [
            "Single model per run; results do not generalize to other models.",
            "Local CPU timing is machine/load dependent; hardware acceleration is not verified.",
            "Exact-name questions only; ambiguous names and paraphrased queries are excluded.",
            "Term matching is lexical, not semantic; no-data detection can flag unrelated negations.",
            "Errors and max-turn stops count as failures; tool results are capped at 6000 characters.",
            "Two cases share each sampled unit; the Wilson interval does not adjust for clustering.",
        ],
    }
    summary["counts"].update({
        "available": len(document["cases"]), "selected": len(cases),
        "resumed": resumed, "newly_evaluated": len(cases) - resumed,
    })
    write_json(out, summary)
    return summary


def load_public_runtime(document: dict[str, Any]) -> tuple[list[dict[str, Any]], ToolCaller, dict[str, Any]]:
    """Bind the real public MCP to the sampled DB in read-only serving mode."""
    db = document["db"]
    path = Path(db["path"]).resolve(strict=True)
    stat = path.stat()
    if (stat.st_size, stat.st_mtime_ns) != (db["size_bytes"], db["mtime_ns"]):
        raise ValueError("Database changed since build-cases; rebuild the case file")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("SELECT 1")
    # Set before the lazy import; never log settings or environment values.
    os.environ.update({
        "NCS_DB_PATH": str(path), "NCS_MCP_READ_ONLY": "1",
        "NCS_MCP_ENABLE_OPERATOR_TOOLS": "0", "NCS_MCP_ENABLE_ADVANCED_TOOLS": "0",
    })
    from ncs_mcp import server, tool_registry

    listed = asyncio.run(server.mcp.list_tools())
    allowed = tool_registry.mcp_tools_for_mode(
        operator_tools_enabled=False, advanced_tools_enabled=False,
    )
    if not listed or any(tool.name not in allowed for tool in listed):
        raise ValueError("MCP runtime is not a public-only tool surface")
    server_file = Path(server.__file__).resolve()
    try:
        shown = server_file.relative_to(ROOT).as_posix()
    except ValueError:
        shown = server_file.name
    identity = {"server_module": shown,
                "server_module_sha256": hashlib.sha256(server_file.read_bytes()).hexdigest()}
    return ollama_tools(listed), server.mcp.call_tool, identity


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-cases", help="Sample known answers from a read-only DB")
    build.add_argument("--db", type=Path, default=ROOT / "data/processed/ncs.db")
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--per-major", type=positive_int, default=1)
    build.add_argument("--seed", type=int, default=SEED)
    run = commands.add_parser("run", help="Evaluate public MCP tool chaining with Ollama")
    run.add_argument("--cases", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--model", default="qwen3:4b")
    run.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    run.add_argument("--max-turns", type=positive_int, default=6)
    run.add_argument("--limit", type=positive_int, help="Evaluate the first N cases, including resumed cases")
    run.add_argument("--fresh", action="store_true", help="Replace prior case results")
    args = parser.parse_args(argv)
    try:
        if args.command == "build-cases":
            if args.out.resolve() == args.db.resolve():
                raise ValueError("Output must not overwrite the database")
            document = build_cases(args.db, args.per_major, args.seed)
            write_json(args.out, document)
            print(f"Built {len(document['cases'])} cases.")
        else:
            document = json.loads(args.cases.read_text(encoding="utf-8"))
            validate_cases(document)
            tools, caller, identity = load_public_runtime(document)
            summary = asyncio.run(run_evaluation(
                document, args.cases, args.out, tools, tool_caller=caller,
                model=args.model, ollama_url=args.ollama_url, max_turns=args.max_turns,
                limit=args.limit, fresh=args.fresh, runtime_identity=identity,
            ))
            print(json.dumps(summary["counts"], sort_keys=True))
        return 0
    except Exception as exc:
        # Do not echo exception payloads, HTTP bodies, or environment secrets.
        print(f"Setup/output error ({type(exc).__name__}). Check inputs, runtime and resume configuration.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
