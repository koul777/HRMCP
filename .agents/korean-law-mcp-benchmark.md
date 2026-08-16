# Korean Law MCP Benchmark Guidance

Use `C:\Users\dd\Downloads\korean-law-mcp-main.zip` as the north-star reference
for MCP maturity, not as code to copy directly.

## What To Emulate

- Compact public tool surface over a larger internal tool set.
- Central tool registry with descriptions, categories, aliases, and execution
  policy.
- Meta discovery and safe meta execution.
- High-level facade tools for real user workflows.
- STDIO and HTTP transports, health endpoint, Docker, CI, release checklist,
  and tool-contract export.
- Typed validation, structured errors, retry/timeout handling, and secret
  masking.
- Durable user-facing docs, API docs, roadmap, changelog-style progress notes,
  and smoke-test artifacts.

## NCS-Specific Translation

- Keep active scope NCS-centered: classifications, units, elements, criteria,
  KSA ontology, training courses, career paths, qualifications, and job-base
  competencies.
- Keep SQF and NCS learning modules as legacy/reference paths unless explicitly
  reactivated.
- Public tools should answer career-development and task-transition training
  questions, not expose every importer or maintenance command.
- Operator/review tools stay hidden from the default public MCP surface. Expose
  them only in explicit operator sessions with `NCS_MCP_ENABLE_OPERATOR_TOOLS=1`,
  and never through `ncs_execute_tool`.
- Recommendation meta-calls should stay read-only and force `save=false`.

## Current Priority

1. Reduce transition recommendation and seedpack latency.
2. Build or refine one benchmark-grade facade workflow only if it improves
   actual career-transition UX.
3. Add trusted transition gold scenarios for evaluation.
4. Preserve fail-free quality gates and report any remaining warnings.
5. Leave evidence after every block: report, command output, DB count, or test.
