# NCS MCP Release Checklist

Use this checklist before registering the NCS MCP server in a local, Docker, or
internal MCP client environment. The active release scope is NCS-centered AI-HR
education planning. SQF and NCS learning-module flows are legacy/reference
surfaces unless the operator explicitly reactivates them.

## 0. Publication Level

Use `private/draft developer preview` when the engineering surface is verified
but release-readiness still has disclosed review or data-coverage blockers. The
minimum preview evidence is:

- `lint`, `smoke`, and unit tests pass.
- Dashboard verification is `ok=true`.
- Release-readiness has `engineering_hygiene_ok=true`.
- Agent queue status/run artifacts are aligned with the release queue.
- Remaining blockers are documented and do not require automated
  `human_reviewed`, `accepted`, or `reviewed` status changes.

CI enforces encoding, unit, lint, and smoke checks. The source-boundary audit is
available as a `workflow_dispatch` deployment gate with
`enforce_source_boundary=true`; run it before pushing or sharing a source-only
preview branch. Dashboard verification and release-readiness evidence are
manual release gates unless a dedicated preview-evidence workflow is added.

Use `stable/internal release` only when the active release-readiness artifact has
`release_ready=true`. Do not use public/stable wording while human review debt,
qualification coverage, or provenance reconfirmation remains open.

Internal deployment and external source distribution are separate decisions.
The repository currently has no root license declaration or package license
metadata. Do not publish it as an open-source or redistributable package until
an authorized owner selects the license and confirms required third-party
notices; automation must not make that legal decision.

## 1. Secrets And Data Boundary

- `.env` is not committed.
- API keys are not printed in logs, reports, `/health`, `/ready`, or MCP tool
  responses.
- Institution-facing serving processes use `NCS_MCP_READ_ONLY=1`; health smoke
  must report `read_only_mode=true` while using an ephemeral prepared database.
- Unauthenticated HTTP transport stays on loopback by default. A non-loopback
  bind requires `--allow-remote-bind` and an institution-controlled private
  gateway providing TLS and access control.
- Start a single serving process with at most two concurrent recommendation
  workflows unless a target-host capacity benchmark supports a higher value.
  Verify overload returns retryable `service_busy` instead of exhausting the
  process.
- Raw CSV/Excel files, generated SQLite DBs, and large reports are not copied
  into Docker images.
- GitHub developer preview commits include source, tests, scripts,
  configuration templates, public MCP contract files, and release docs only.
  Keep generated reports as private release evidence or local artifacts unless
  a specific small summary is intentionally committed.
- Generated SQLite databases are not part of the source preview package. If a
  prepared DB is required for reviewers, provide it through a controlled private
  LFS/artifact handoff and document the expected `NCS_DB_PATH`.
- Docker runs mount `data/processed` as a volume.
- Clean CI and Docker builds run `python -m pip check` after installation.
- Before a stable internal release, archive the exact resolved dependency list,
  container image digest, and institution-required software bill of materials or
  vulnerability scan. Version ranges in `pyproject.toml` are package
  compatibility bounds; they are not a reproducible production lock.

Before pushing a GitHub preview branch, run the source-boundary audit. The
preview branch must pass with `ok=true`.

```powershell
python scripts\build_deployment_source_manifest.py --json-out reports\deployment_source_manifest_<DATE>.json --markdown-out reports\deployment_source_manifest_<DATE>.md
python scripts\check_deployment_source_boundary.py --check-lfs-history --json-out reports\deployment_source_boundary_<DATE>.json --markdown-out reports\deployment_source_boundary_<DATE>.md
```

If the current worktree is intentionally dirty, prepare a non-destructive
source-only preview tree under `tmp/` instead of pushing the branch directly.
The exporter copies tracked source files by default; include untracked source
candidates only after reviewing each path.

```powershell
python scripts\export_deployment_source_preview.py --json-out reports\deployment_source_preview_export_<DATE>.json --markdown-out reports\deployment_source_preview_export_<DATE>.md
python scripts\export_deployment_source_preview.py --include-untracked src\ncs_mcp\query_router.py --json-out reports\deployment_source_preview_export_<DATE>.json --markdown-out reports\deployment_source_preview_export_<DATE>.md
```

After reviewing and exporting the intended source candidates, verify the exact
copied tree, run source-only smoke checks with a temporary generated DB, and
scan the export for blocked artifacts or secrets. These commands do not call
external APIs or modify the Git index. The runtime smoke verifies both STDIO and
HTTP `/health` and `/ready` surfaces, performs an offline temporary package
install and CLI check, and terminates its temporary HTTP process.

```powershell
python scripts\verify_deployment_source_preview.py --source-preview-export reports\deployment_source_preview_export_<DATE>.json --out reports\deployment_source_preview_tree_verification_<DATE>.json --markdown-out reports\deployment_source_preview_tree_verification_<DATE>.md
python scripts\run_deployment_source_preview_smoke.py --output-dir tmp\deployment_source_preview_<DATE> --out reports\deployment_source_preview_runtime_smoke_<DATE>.json --markdown-out reports\deployment_source_preview_runtime_smoke_<DATE>.md
python scripts\scan_source_preview_artifacts.py --output-dir tmp\deployment_source_preview_<DATE> --out reports\deployment_source_preview_scan_<DATE>.json --markdown-out reports\deployment_source_preview_scan_<DATE>.md
```

For a first publication to a new remote, also run with `--fail-on-lfs-history`.
If it fails, split or rewrite the publication branch before uploading old raw
workbook or generated DB LFS objects.

Required or optional API keys:

- `NCS_SERVICE_KEY`
- `NCS_TRAINING_COURSE_SERVICE_KEY`
- `NCS_QUALIFICATION_SERVICE_KEY`
- `NCS_JOB_BASE_SERVICE_KEY`

Check service exposure without printing key values:

```powershell
python scripts\mcp_http_health_smoke.py --timeout 20
```

## 2. Local Verification

Run from the repository root.

```powershell
python -m py_compile src\ncs_mcp\server.py src\ncs_mcp\tool_registry.py src\ncs_mcp\error_codes.py src\ncs_mcp\helpers.py scripts\ncs_harness.py scripts\mcp_stdio_smoke.py scripts\mcp_http_health_smoke.py scripts\export_mcp_tool_contract.py
python -m unittest tests.test_http_client tests.test_training_recommendation tests.test_ncs_mcp tests.test_harness -v
python scripts\ncs_harness.py lint
python scripts\export_mcp_tool_contract.py --check --out mcp\ncs-tool-contract.json
python scripts\mcp_stdio_smoke.py --timeout 15
python scripts\mcp_http_health_smoke.py --timeout 20
python scripts\package_install_smoke.py --source-preview-dir tmp\deployment_source_preview_<DATE> --out reports\source_package_install_smoke_<DATE>.json --markdown-out reports\source_package_install_smoke_<DATE>.md
python scripts\prepare_serving_database.py --source-db <active-ncs.db> --output-db <new-serving-ncs.db> --out reports\serving_database_snapshot_<DATE>.json --markdown-out reports\serving_database_snapshot_<DATE>.md --quick-check
python scripts\benchmark_chatbot_readiness.py --db <prepared-ncs.db> --out reports\institutional_chatbot_readiness_benchmark_<DATE>.json --markdown-out reports\institutional_chatbot_readiness_benchmark_<DATE>.md
python scripts\ncs_harness.py smoke
```

When ontology or recommendation evidence changes, also run:

```powershell
python scripts\ncs_harness.py ontology validate
python scripts\ncs_harness.py quality-gates --include-transition-eval --transition-limit 5
```

Before handing review artifacts to an operator or attaching them to release
evidence, run a report-only readability audit:

```powershell
python scripts\ncs_harness.py audit-review-artifact-readability --reports-dir reports --out reports\review_artifact_readability_audit_20260629.json --markdown-out reports\review_artifact_readability_audit_20260629.md
```

This check only verifies artifact readability and display noise. It is not
human approval and must not set `human_reviewed`, `accepted`, or `reviewed`.
If Korean text appears corrupted only in a terminal/editor, re-open the artifact
as UTF-8 and run a focused audit before treating it as source-data corruption.
When the audit is passed into release readiness with
`--review-readability-audit`, only findings that overlap the current release
proof/dashboard static artifacts become blockers. Historical findings outside
that active proof set remain review debt. Use `--strict` only for a focused
artifact list when any readability finding should fail the command.

## 3. HTTP Runtime

```powershell
.\run_ncs_mcp_http.cmd
```

Default endpoints:

- MCP: `http://127.0.0.1:8766/mcp`
- health: `http://127.0.0.1:8766/health`
- readiness: `http://127.0.0.1:8766/ready`

`/health` and `/ready` may expose only:

- MCP tool count.
- Legacy tool exposure count.
- DB configured/exists/openable/ready booleans and core table row counts.
- API key presence booleans, never key values.

`/health` is a process liveness surface and can return `status=degraded` when
the DB is not ready. `/ready` must return 503 when the DB is missing or core
lookup tables are empty.

## 4. Docker Runtime

```powershell
docker build -t ncs-mcp:local .
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp -p 127.0.0.1:8766:8766 -v ${PWD}\data\processed\ncs.db:/data/ncs.db:ro -e NCS_MCP_HOST=0.0.0.0 -e NCS_MCP_ALLOW_REMOTE_BIND=1 ncs-mcp:local
```

Optional smoke DB check:

```powershell
mkdir docker-smoke
docker run --rm -v ${PWD}\docker-smoke:/data ncs-mcp:local python -m ncs_mcp.smoke_data --out /data/ncs.db
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp -p 127.0.0.1:8766:8766 -v ${PWD}\docker-smoke\ncs.db:/data/ncs.db:ro -e NCS_MCP_HOST=0.0.0.0 -e NCS_MCP_ALLOW_REMOTE_BIND=1 ncs-mcp:local
```

When Docker CLI is unavailable locally, use CI Docker build and container
readiness smoke jobs as the release evidence.

## 5. Client Registration And Tool Contract

STDIO client example:

```json
{
  "mcpServers": {
    "ncs-training": {
      "command": "C:\\workspace\\NCS_MCP\\run_ncs_mcp_stdio.cmd"
    }
  }
}
```

HTTP client example:

```json
{
  "mcpServers": {
    "ncs-training-http": {
      "url": "http://127.0.0.1:8766/mcp"
    }
  }
}
```

Export the public tool contract:

```powershell
python scripts\export_mcp_tool_contract.py --out mcp\ncs-tool-contract.json
```

## 6. Active Product Scope

- Active: NCS structure search, KSA/task ontology, training-course
  recommendations, career transition planning, career-path/qualification/job
  base evidence.
- Hidden or legacy by default: SQF tools and NCS learning-module tools.
- Operator/review tools must not be executable through `ncs_execute_tool`.
- Recommendation tools executed through `ncs_execute_tool` must force
  `save=false`.
- Productization scope, buyer/user assumptions, API-key ownership, and deferred
  public SaaS boundaries are documented in
  `docs/AIHR_PRODUCTIZATION_STRATEGY.md`.
- Deployment modes, startup commands, owner boundaries, and rollback steps are
  documented in `docs/AIHR_DEPLOYMENT_RUNBOOK.md`.

## 7. Client Response Conventions

- Recommendation tools should default to compact public responses.
- Clients should branch on `error.code` or `error.category`, not on display
  text.
- `[NOT_FOUND]` is an LLM guidance marker, not a stable client condition.
- `external_dependency` errors with `retryable=true` may be retried by the
  client; other errors should be surfaced to an operator.

## 8. AI-HR Route And Demo Contract Gate

The live AI-HR planner and demo artifacts must expose the route contract. A
missing route contract is a release failure even when the training-system
matrix renders.

Required route checks:

- `query_route.schema == ncs_query_route_v1`.
- `query_route.tool == plan_ncs_education_path` for AI-HR education-path
  scenarios.
- `expected_tool_chain`, `route_contract`, and `route_fingerprint` are present
  and non-empty.
- Dashboard verification reports `missing_query_route_fields=[]`.
- Release readiness reports no `aihr_dashboard_surface` or
  `aihr_demo_contract` blocker.

Recommended proof chain:

```powershell
python scripts\ncs_harness.py route-ncs-query "labor management to HR planning education path"
python scripts\ncs_harness.py run-aihr-plan-demo --out-dir reports --base-name aihr_plan_demo_20260624
python scripts\ncs_harness.py verify-aihr-dashboard --base-url http://127.0.0.1:8765 --out reports\aihr_dashboard_surface_verification_20260624.json --markdown-out reports\aihr_dashboard_surface_verification_20260624.md
python scripts\release_readiness_report.py --quality-report reports\aihr_quality_gates_with_transition_20260624.json --contract reports\mcp_tool_contract_20260624.json --demo-json reports\aihr_plan_demo_20260624.json --demo-json reports\aihr_plan_demo_alias_20260624.json --demo-html reports\aihr_plan_demo_20260624.html --dashboard-verification reports\aihr_dashboard_surface_verification_20260624.json --out reports\aihr_release_readiness_20260624.json --markdown-out reports\aihr_release_readiness_20260624.md
```

If any public or internal demo JSON has `query_route.tool=null`, stop the
release and regenerate the demo after fixing the route attachment path.

## 9. Qualification API Collection Guard

Qualification evidence may be partial before release, but collection jobs must
be resumable and must not hammer the upstream API.

Required checks:

- Run `python scripts\ncs_harness.py qualification-summary --limit 10` and keep
  the current coverage/error concentration in the release report.
- Run `python scripts\ncs_harness.py qualification-coverage-plan --target-ratio 0.9 --ncs006-checkpoint-path reports\checkpoint_ncs006_element_api_status_<DATE>_current.json`
  to calculate guarded all-unit batch needs before broad collection.
- Run `python scripts\ncs_harness.py qualification-retry-hygiene --limit 50 --ncs006-checkpoint-path reports\checkpoint_ncs006_element_api_status_<DATE>_current.json`
  before retrying cached errors.
- Run `python scripts\checkpoint_ncs006_element_api_status.py` before any
  guarded API collection when NCS006 collection has recent rate-limit history.
- Do not run guarded API collection when agent queue status is
  `blocked_safety` or `can_start_automated=false`.
- Use `--stop-after-rate-limit-errors` for every broad
  `retry-qualification-errors` or `collect-qualification-items --all-units`
  batch.
- Treat `stopped_early=true` or `stop_reason=rate_limited` as a hard stop for
  that collection wave.
- Do not use `--include-not-due`, `--refresh`, or very large `--limit-units`
  values during routine recovery.

Safe retry template after safety blockers are cleared:

```powershell
python scripts\ncs_harness.py retry-qualification-errors --limit-units 10 --num-of-rows 50 --max-pages 1 --request-delay 3 --max-retries 1 --retry-backoff-seconds 120 --stop-after-rate-limit-errors 2 --ncs006-checkpoint-path reports\checkpoint_ncs006_element_api_status_<DATE>_current.json
```

Safe new-coverage template after safety blockers are cleared:

```powershell
python scripts\ncs_harness.py collect-qualification-items --all-units --limit-units 100 --num-of-rows 50 --max-pages 1 --request-delay 2 --max-retries 1 --retry-backoff-seconds 30 --stop-after-rate-limit-errors 3 --ncs006-checkpoint-path reports\checkpoint_ncs006_element_api_status_<DATE>_current.json
```

Release readiness remains false until `scripts\release_readiness_report.py`
reports the configured qualification coverage threshold as satisfied.

## 10. SQLite Operating Boundary

The production knowledge graph is a generated SQLite file. It is acceptable for
the current read-heavy internal release target, but it is not a general
multi-writer or public SaaS storage plan.

Required release checks:

- Record DB size and sidecar state:

```powershell
Get-ChildItem data\processed -Filter "ncs.db*" | Select-Object Name,Length,LastWriteTime
```

- Confirm the DB is mounted as an external volume for Docker/internal hosting
  and is not copied into the image.
- Confirm broad API collection, preprocessing, and review imports are not run
  against the same live DB while shared planner traffic is being served.
- Confirm live planner/demo/dashboard verification uses no-save/read-only paths
  unless an operator intentionally starts a guarded write job.
- Confirm backup/restore procedure accounts for `ncs.db`, `ncs.db-wal`,
  `ncs.db-shm`, and `ncs.db-journal` sidecars. Do not delete sidecars while DB
  users are running.
- Do not hand off the main file alone from an active WAL database. Use
  `prepare_serving_database.py` to merge committed WAL content into a new,
  closed `DELETE`-journal snapshot, retain its SHA-256 report, and benchmark
  that exact file. The snapshot command writes only the explicitly named new
  destination and never overwrites the source or an existing destination.

Current observed local size on 2026-06-23:

- `data/processed/ncs.db`: 12,648,931,328 bytes.
- `data/processed/ncs.db-shm`: 32,768 bytes.
- `data/processed/ncs.db-wal`: 32,992 bytes.

Escalate to a server-grade database or separate replicated read model when any
of these are true:

- More than one regular writer is needed.
- SQLite busy timeouts or write locks occur during normal shared planner use.
- Real-time collection must overlap live recommendations.
- Backup/restore or file transfer for the single DB misses the recovery window.
- Query latency from large joins exceeds the product SLO after indexing and
  compact-response tuning.

## 11. Release Decision

Release can be called ready only when:

- `release_ready=true` in the dated release-readiness report.
- `engineering_hygiene_ok=true`.
- `blocker_count=0`.
- Dashboard verification has `ok=true` and `failure_count=0`.
- Public demo JSON/HTML and internal demo JSON route checks pass.
- Review seedpacks remain export-only unless a separate human decision apply
  step has explicit reviewer id, rationale, timestamp, and source packet.
