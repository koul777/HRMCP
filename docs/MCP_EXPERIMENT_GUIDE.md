# MCP Experiment Guide

The active MCP is an NCS-centered training recommendation server. It exposes a
compact tool surface for NCS structure search, training recommendation, evidence
inspection, and transition analysis. Operator review tools require explicit
operator mode. SQF and NCS study-module tools remain legacy reference paths and
are intentionally hidden from the active MCP surface.

The exposed tool catalog is defined in `src\ncs_mcp\tool_registry.py`. The server
uses that registry for discovery, surface checks, HTTP health metadata, and
legacy-tool removal.
The public JSON contract is generated from the same registry at
`mcp\ncs-tool-contract.json`.
Use `docs\MCP_RELEASE_CHECKLIST.md` before registering or deploying the MCP.
For a Korean user-facing overview of the 10 public tools, see
`docs\NCS_MCP_USER_GUIDE_KO.md`.

## Run Modes

Local STDIO mode remains the default for desktop MCP clients:

```powershell
.\run_ncs_mcp_stdio.cmd
```

Streamable HTTP mode is available for local experiments and deployment work:

```powershell
.\run_ncs_mcp_http.cmd
```

The HTTP endpoint defaults to:

- MCP endpoint: `http://127.0.0.1:8766/mcp`
- health endpoint: `http://127.0.0.1:8766/health`
- readiness endpoint: `http://127.0.0.1:8766/ready`

The health response includes tool counts, database readiness, and API-key
presence booleans. `/health` returns liveness metadata and reports
`status=degraded` when the DB is not ready. `/ready` is strict and returns 503
until the DB exists, opens read-only, and has rows in the core NCS/training
tables. Neither endpoint returns API-key values.

Override the bind address with environment variables:

```powershell
$env:NCS_MCP_HOST="127.0.0.1"
$env:NCS_MCP_PORT="8766"
.\run_ncs_mcp_http.cmd
```

Run a real STDIO protocol smoke test:

```powershell
python scripts\mcp_stdio_smoke.py --timeout 15
```

Run a real HTTP process health smoke test:

```powershell
python scripts\mcp_http_health_smoke.py --timeout 20
```

Regenerate or check the public tool contract:

```powershell
python scripts\export_mcp_tool_contract.py --out mcp\ncs-tool-contract.json
python scripts\export_mcp_tool_contract.py --check --out mcp\ncs-tool-contract.json
```

Build and run the HTTP server with Docker:

```powershell
docker build -t ncs-mcp:local .
docker run --rm -p 8766:8766 -v ${PWD}\data\processed:/data ncs-mcp:local
```

For a minimal Docker readiness smoke without mounting the full production DB:

```powershell
mkdir docker-smoke
docker run --rm -v ${PWD}\docker-smoke:/data ncs-mcp:local python -m ncs_mcp.smoke_data --out /data/ncs.db
docker run --rm -p 8766:8766 -v ${PWD}\docker-smoke:/data ncs-mcp:local
```

Pass API keys with environment variables when collection utilities are needed.
Do not bake `.env`, raw CSV/Excel files, or generated SQLite databases into the
image.

## Operational Safety

MCP tool errors use the common `ok/data/error/audit` envelope. Error fields are
masked before they leave the server boundary, including query parameters such as
`authKey`, `serviceKey`, `apiKey`, and known NCS service-key environment values.
Specific not-found codes such as `concept_not_found` remain machine-readable in
`error.code`; the `[NOT_FOUND]` text marker is only user/LLM guidance.
See `docs\MCP_ERROR_CODES.md` for the error category and retryability catalog.

External API collection helpers use bounded retries for transient request
errors and HTTP `429`, `502`, `503`, and `504`. Qualification collection keeps
its per-unit retry status table; training-course and job-base collection use the
shared `src\ncs_mcp\http_client.py` helper.

Before retrying qualification API errors at scale, run the retry hygiene report.
Use `--apply` only to backfill retry metadata from existing error rows; it does
not call the API.

```powershell
python scripts\ncs_harness.py qualification-retry-hygiene --out reports\qualification_retry_hygiene.json --markdown-out reports\qualification_retry_hygiene.md
python scripts\ncs_harness.py qualification-retry-hygiene --apply --retry-backoff-seconds 3600 --out reports\qualification_retry_hygiene_applied.json --markdown-out reports\qualification_retry_hygiene_applied.md
```

Then retry only a small API batch after `next_retry_at` has passed:

```powershell
python scripts\ncs_harness.py retry-qualification-errors --limit-units 50 --num-of-rows 50 --max-pages 1 --request-delay 2 --max-retries 1 --retry-backoff-seconds 30 --stop-after-rate-limit-errors 3 --report-path reports\qualification_error_report.md
```

Treat `stopped_early=true` or `stop_reason=rate_limited` as a hard stop for the
current collection wave. Do not widen the retry batch until the API retry window
has passed and `qualification-retry-hygiene` shows retry-ready rows again.

Saved recommendation evidence can also be checked for stale training-goal link
references after ontology/link rebuilds:

```powershell
python scripts\ncs_harness.py recommendation-evidence-hygiene --out reports\recommendation_evidence_hygiene.json --markdown-out reports\recommendation_evidence_hygiene.md
python scripts\ncs_harness.py recommendation-evidence-hygiene --apply --out reports\recommendation_evidence_hygiene_applied.json --markdown-out reports\recommendation_evidence_hygiene_applied.md
```

The apply path updates only saved recommendation evidence `source_id` values
that can be remapped to current `training_goal_concept_links`.

`ncs_execute_tool` is read-only by policy. It blocks meta-recursion, blocks
operator/review tools, forces `save=false` for recommendation calls, and converts
unexpected handler exceptions into `tool_execution_failed` without exposing API
keys.

## Discover Tools

Use `ncs_discover_tools` first when the right tool is unclear.

```json
{"tool": "ncs_discover_tools", "arguments": {"intent": "career transition training recommendation"}}
```

For read-only user tools, `ncs_execute_tool` can execute a discovered tool by
name. Recommendation tools called this way force `save=false`, so exploratory
meta calls do not create recommendation audit rows. Recommendation tools also
default to `compact=true` through `ncs_execute_tool` unless the caller explicitly
sets `compact=false`.

```json
{
  "tool": "ncs_execute_tool",
  "arguments": {
    "tool_name": "recommend_training_transition",
    "params": {
      "current_query": "노무관리",
      "target_query": "인사기획",
      "limit": 3,
      "compact": true
    }
  }
}
```

Operator review tools are hidden in the default public MCP surface. Start the
server with `NCS_MCP_ENABLE_OPERATOR_TOOLS=1` before launch when an explicit
review action is intended. They remain blocked from `ncs_execute_tool`.

## User Tools

Search NCS structures:

```json
{"tool": "ncs_search", "arguments": {"query": "인력채용", "scope": "unit", "limit": 5}}
```

Inspect a unit:

```json
{"tool": "ncs_unit_detail", "arguments": {"unit_code": "0202020101_23v3", "include": ["elements", "criteria", "ksa", "training", "qualification"]}}
```

Search training courses:

```json
{"tool": "ncs_training", "arguments": {"query": "인력채용", "limit": 5}}
```

Recommend training from a task:

```json
{"tool": "recommend_training_for_task", "arguments": {"query": "인력채용", "mode": "all", "limit": 5, "compact": true}}
```

Recommend training for a career transition:

```json
{"tool": "recommend_training_transition", "arguments": {"current_query": "노무관리", "target_query": "인사기획", "limit": 5, "compact": true}}
```

Explain related task transitions:

```json
{"tool": "recommend_task_transitions", "arguments": {"query": "인력채용", "mode": "all", "limit": 5}}
```

Inspect supporting evidence:

```json
{"tool": "ncs_analysis", "arguments": {"mode": "qualification", "unit_code": "0202020101_23v3", "limit": 10}}
```

```json
{"tool": "get_concept_evidence", "arguments": {"concept_id": 1, "limit": 10}}
```

## Operator Tools

Use these only for explicit review or quality work, and only after starting the
server with `NCS_MCP_ENABLE_OPERATOR_TOOLS=1`:

```json
{"tool": "get_quality_issues", "arguments": {"limit": 20}}
```

```json
{"tool": "review_ontology_concept", "arguments": {"concept_id": 1, "review_status": "human_reviewed", "reviewer_id": "mcp"}}
```

```json
{"tool": "review_training_goal_concept_link", "arguments": {"link_id": 1, "review_status": "human_reviewed", "reviewer_id": "mcp"}}
```

```json
{"tool": "review_task_ksa_concept_relation", "arguments": {"relation_id": 1, "review_status": "rejected", "reviewer_id": "mcp"}}
```

```json
{"tool": "review_learning_module_ncs_link", "arguments": {"link_id": 1, "review_status": "human_reviewed", "reviewer_id": "mcp"}}
```
