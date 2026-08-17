# AI-HR Deployment Runbook

This runbook turns the AI-HR productization strategy into an operational path
for the current NCS MCP release.

## Supported deployment modes

### 1. Local analyst pilot

- Purpose: individual analyst or planner evaluation.
- Transport: STDIO or localhost HTTP.
- DB: prepared SQLite at `data/processed/ncs.db`.
- API keys: owned by the operator; not exposed to end users.
- Collection: disabled during normal analyst use.

Recommended startup:

```powershell
python scripts\ncs_harness.py smoke
python scripts\ncs_dashboard.py --host 127.0.0.1 --port 8765
```

### 2. Internal team service

- Purpose: shared HRD planning workflow for a controlled team.
- Transport: private HTTP MCP.
- DB: mounted SQLite volume.
- API keys: owned by platform/ops or a designated data operator.
- Collection: guarded jobs only, separated from serving.

Recommended startup:

```powershell
.\run_ncs_mcp_http.cmd
python scripts\mcp_http_health_smoke.py --timeout 20
```

### 3. Containerized internal deployment

- Purpose: self-contained internal hosting with operational controls.
- Transport: Docker.
- DB: prepared SQLite file mounted read-only at `/data/ncs.db`.
- API keys: injected by the runtime environment, never printed.
- Collection: run as a separate guarded job, not inside the serving container.

Recommended startup:

```powershell
python scripts\prepare_serving_database.py --source-db data\processed\ncs.db --output-db C:\secure-data\ncs.db --out reports\serving_database_snapshot.json --markdown-out reports\serving_database_snapshot.md --quick-check
$env:NCS_DB_HOST_PATH="C:\secure-data\ncs.db"
docker compose -f deploy\compose.internal.yml up --build -d
```

The Compose file binds the host port only to `127.0.0.1`, mounts the prepared
database file read-only, removes Linux capabilities, and uses a read-only root
filesystem. Put the institution TLS/auth gateway in front of that loopback
endpoint. Do not replace this with an all-interface Docker port mapping or a
writable serving-data directory mount.

## Ownership model

| Area | Owner | Rule |
| --- | --- | --- |
| Product scope and wording | Product owner | Do not claim official qualification recognition or legal eligibility. |
| MCP hosting and access | Platform/ops | Keep exposure private and controlled. |
| API keys | Platform/ops or data operator | HR users do not receive raw service keys. |
| Broad API collection | Data operator | Use guarded retries and rate-limit checkpoints. |
| Recommendation QA | Engineering/QA | Verify contract, lint, smoke, tests, and release-readiness reports. |
| Human review decisions | HR reviewer/domain expert | Human approval is required for review-state promotion. |

## Release sequence

1. Confirm `query_route.tool=plan_ncs_education_path` on the demo and dashboard
   surfaces.
2. Verify the public tool contract and dashboard verification reports.
3. Check qualification coverage and the NCS006 checkpoint state.
4. Review ontology definition, training-goal link, and task-KSA seedpacks.
5. Publish only after the release-readiness report is green for the current
   scope and all remaining limitations are disclosed.

### Developer preview exception

If `release_ready=false` but `engineering_hygiene_ok=true`, the repository may
be shared only as a private or draft developer preview. The preview note must
state that human-review decisions and qualification coverage are still open,
and it must link the active release-readiness, dashboard verification, queue
status/run, and remaining-blockers artifacts. Do not present this state as an
approved HR recommendation system or a stable public release.

For the GitHub preview package, commit only reproducible source material:
application code, tests, harness scripts, documentation, configuration
templates, Docker/CI metadata, and the public MCP tool contract. Do not commit
`.env`, raw source downloads, generated SQLite databases, report directories,
exports, logs, caches, or virtual environments. Keep the active evidence files
under `reports/` as ignored local artifacts or attach them to a private draft
release with the same filenames referenced from the preview note.

Do not push from a dirty working tree that still shows tracked or staged
`data/raw`, `data/processed`, `reports`, `exports`, or `tmp` paths. Prepare a
clean deployment branch and remove generated artifacts from the branch index
before the first GitHub push. If this repository is being published to a new
remote for the first time and old commits contain raw workbook or generated DB
LFS objects, split the deployment branch or rewrite that publication history so
those objects are not uploaded.

Run the deployment source-boundary audit before pushing:

```powershell
python scripts\check_deployment_source_boundary.py --check-lfs-history --json-out reports\deployment_source_boundary_<DATE>.json --markdown-out reports\deployment_source_boundary_<DATE>.md
```

To prepare a reviewable include/exclude list without staging anything, generate
the source manifest:

```powershell
python scripts\build_deployment_source_manifest.py --json-out reports\deployment_source_manifest_<DATE>.json --markdown-out reports\deployment_source_manifest_<DATE>.md
```

When the product evidence is preview-ready but the current branch still contains
tracked generated blockers, create a non-destructive source preview tree under
`tmp/` and attach its evidence to the preview note. The exporter is tracked-only
by default; every untracked file must be included with an explicit
`--include-untracked <path>` argument after review.

```powershell
python scripts\export_deployment_source_preview.py --json-out reports\deployment_source_preview_export_<DATE>.json --markdown-out reports\deployment_source_preview_export_<DATE>.md
```

Use `--fail-on-lfs-history` before the first publication to a new remote. A
failed audit is a packaging blocker, not a product-quality blocker; keep the
generated evidence local or attach it to a private draft release after the
source branch is clean.

If reviewers need a prepared `data/processed/ncs.db`, distribute it separately
through a controlled private LFS/artifact handoff and document the exact
`NCS_DB_PATH` they should use. The source preview should still be runnable with
an operator-supplied DB or with the documented preprocessing flow.

## Rollback and safety

- If the dashboard or demo loses the route contract, stop the release and
  regenerate the affected artifacts.
- If qualification collection hits rate limits, keep the watchdog guarded and
  do not start duplicate collectors.
- If a file or report would require writing `human_reviewed`, `accepted`, or
  `reviewed`, stop and wait for a human decision.
