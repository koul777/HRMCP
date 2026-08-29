# Vercel compact snapshot Builder

The preferred replacement path is the one-input Publisher:

```powershell
python scripts\publish_vercel_snapshot.py --source data\processed\ncs.db
```

It stages and verifies the compact snapshot, rechecks the canonical source
hash, and atomically publishes the verified ZIP/manifest pair to the selected
Vercel app's `api` directory. A publication failure restores the complete
previous pair. Optional controls are `--deploy-root`, `--dry-run`, and
`--report`.

`scripts/build_vercel_snapshot.py` remains the low-level, deterministic Builder
for custom output paths. It takes one prepared canonical SQLite database and
creates a fresh compact SQLite database, ZIP archive, manifest, and build
report. It does not deploy to Vercel or publish into the deploy root.

The current deployed artifact chain is:

```text
data/processed/ncs.db                         12,648,931,328 bytes
  -> deterministic Builder
compact SQLite                                  425,758,720 bytes
  -> package
api/ncs_ontology_compact.zip                   120,785,873 bytes
  -> verified Vercel materialization
/tmp/ncs_ontology_compact.db                   read-only at runtime
```

The Builder is deliberately not AI. It does not embed or call an AI model,
does not call NCS APIs, does not modify the canonical source database, and
does not update human-review statuses. Vercel likewise only verifies and
serves the packaged snapshot; it does not perform API collection at request
time.

## Build a custom staged snapshot

Run from the repository root with paths that do not already exist. The Builder
refuses to replace an output so an existing deploy input is never silently
overwritten.

```powershell
python scripts\build_vercel_snapshot.py `
  --source data\processed\ncs.db `
  --output-db build\ncs_ontology_compact_<DATE>.db `
  --archive build\ncs_ontology_compact_<DATE>.zip `
  --manifest build\ncs_ontology_compact_<DATE>.manifest.json `
  --report reports\vercel_snapshot_build_<DATE>.json
```

For a no-write inspection of resolved paths and exact argument arrays:

```powershell
python scripts\build_vercel_snapshot.py `
  --source data\processed\ncs.db `
  --output-db build\ncs_ontology_compact_<DATE>.db `
  --archive build\ncs_ontology_compact_<DATE>.zip `
  --manifest build\ncs_ontology_compact_<DATE>.manifest.json `
  --report reports\vercel_snapshot_build_<DATE>.json `
  --dry-run
```

The Builder runs only these fixed stages:

1. `export_interview_serving_db.py --profile vercel-ontology-compact`
2. `package_vercel_compact_snapshot.py`
3. `verify_vercel_compact_package.py --skip-function-bundle-check`

It validates the source SQLite header and records stage timing, SHA-256,
artifact sizes, and bounded stdout/stderr tails in the JSON report. The final
verification is archive-only; function bundle measurement and Vercel deployment
are outside the Builder's scope.

## Refresh and publish boundaries

Refreshing the canonical source is an upstream scheduled/guarded pipeline, not
a Builder stage. That pipeline owns API credentials, checkpoints, retries,
quality gates, and operator approval for qualification API collection. Once it
has produced and validated the one canonical `ncs.db`, the Builder can produce
a new staged snapshot from it.

For the standard deployment path, use `publish_vercel_snapshot.py` rather than
manually promoting build outputs. It stages, verifies, and atomically publishes
only the ZIP/manifest pair with rollback. `build_vercel_snapshot.py` is retained
for custom output paths and remains intentionally separate from publication and
Vercel deployment.

The runtime validates the manifest/archive before materializing the SQLite file
under `/tmp`, then opens that path read-only. It does not use `NCS_DB_URL` in
the standard deployment flow.

## Automatic refresh shape

If the canonical source DB changes over time, the stable design is:

```text
upstream API collection / DB refresh
  -> one canonical ncs.db
  -> Publisher stage + verify
  -> atomic ZIP + manifest publish into deploy/vercel_mcp_app/api
  -> Vercel CLI deploy + remote MCP verification
```

That is why the deterministic Publisher/Builder path is better than embedding
AI into the Vercel release path. The release path needs reproducible file
transforms, content checks, rollback, and a clear deploy gate. AI can still
help generate HR outputs through the MCP tools, but it does not decide how the
deployment artifact is built.

Because the canonical source DB is currently about 12.6 GB, the scheduled build
environment needs sufficient disk capacity. The serving side on Vercel remains
lightweight because only the compact ZIP and manifest are deployed.

## Deploy the verified input

```powershell
cd deploy\vercel_mcp_app
vercel deploy
vercel deploy --prod
```

The first command produces a preview deployment; the second deploys production.
Do not use `--prebuilt`. `git.deploymentEnabled=false` prevents Git pushes from
deploying a commit that lacks the ignored ZIP; release only through this CLI
flow after the Publisher has completed. Confirm `/api/health` and `/api/ready`
after deployment.
