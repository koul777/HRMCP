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
  KSA ontology, NCS training API courses, career paths, qualifications, and
  job-base competencies.
- Keep SQF and NCS learning modules as legacy/reference paths unless explicitly
  reactivated.
- Treat the 2026 NCS HR training-system report as workflow/rubric guidance, not
  source data or score-boosting evidence.
- Public tools should answer career-development and task-transition training
  questions, not expose every importer or maintenance command.
- Operator/review tools stay hidden from the default public MCP surface. Expose
  them only in explicit operator sessions with `NCS_MCP_ENABLE_OPERATOR_TOOLS=1`,
  and never through `ncs_execute_tool`.
- Recommendation meta-calls should stay read-only and force `save=false`.
- Natural-language requests should pass through `src/ncs_mcp/query_router.py`
  before agents choose a tool. The route result must include scenario, tool,
  params, missing params, suggested pipeline, and risk flags.
- Law MCP search-detail chains translate to NCS workflow chains:
  query scope -> KSA/task evidence -> training courses -> education-system
  matrix -> readiness/review artifacts.
- AI-HR live/demo/release surfaces must preserve `recommended_path`,
  `training_system_matrix`, `task_ksa_basis`, `facility_constraint_fit`,
  `human_review`, `query_route`, and `training_system_guide_trace`.
- Automation agents must not set `human_reviewed`, `accepted`, or `reviewed`
  without explicit human decisions.
- Prefer the latest readiness JSON's `agent_work_queue_path` when checking
  release evidence. Use one `<DATE>` stamp across queue/status/run artifacts;
  treat `reports/aihr_agent_work_queue_<DATE>.*` as a legacy/alias queue family
  only when referenced by readiness JSON.

## Current Priority

1. Keep `ncs_discover_tools` route-aware and keep `ncs_execute_tool` safe,
   read-only, and auditable.
2. Expand benchmark-grade facade workflows only when they improve actual
   AI-HR education-system UX.
3. Add chain/risk contracts to tests before widening the public tool surface.
4. Add trusted transition gold scenarios for evaluation.
5. Preserve fail-free quality gates and leave evidence after every block:
   report, command output, DB count, or test.
