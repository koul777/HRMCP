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

## Change-aware Refresh Builder

Use the Refresh Builder before the snapshot Publisher when a new `ncs.db` is
supplied. Planning is the default and is read-only:

```powershell
python scripts\refresh_ncs_ontology.py data\processed\ncs.db `
  --state-dir C:\ncs_mcp_state\ncs-ontology-refresh `
  --report reports\ncs_ontology_refresh_plan.json
```

An explicit `--apply` creates a separate prepared database. It never writes to
the supplied DB or the promoted baseline:

```powershell
python scripts\refresh_ncs_ontology.py data\processed\ncs.db `
  --state-dir C:\ncs_mcp_state\ncs-ontology-refresh `
  --output build\prepared\ncs.db `
  --report reports\ncs_ontology_refresh_apply.json `
  --apply
```

The Builder compares stable source projections rather than volatile timestamps.
It chooses one of these fail-closed strategies:

| Detected change | Builder action |
| --- | --- |
| No source projection change | Reuse the last promoted, verified baseline; never publish an unpromoted candidate |
| Small append-only NCS/KSA change | Add missing atomic KSA, concept, task, similarity, and training evidence on a working copy |
| Training-course additions | Add the corresponding training links on a working copy |
| Career, qualification, or job-base evidence only | Prepare the new evidence without rebuilding the core ontology |
| Schema/key conflict, large change, source update/delete, or trusted-row conflict | Block automatic publication and require a guarded rebuild/reconciliation |

`publisher_source` in the successful apply report is the only DB that may move
to the compact snapshot Publisher. The report also records the selected
strategy, affected tables/scopes, source hashes, rule fingerprint, integrity
checks, and KSA/review-state invariants.

## Supplemental API Refresh Builder

Training-course and job-base APIs can be refreshed before ontology planning.
The command discovers all NCS major codes from the DB. It creates a consistent
SQLite online-backup first, including committed WAL frames, and calls the APIs
only against that working copy:

```powershell
python scripts\refresh_ncs_api_evidence.py `
  --db data\processed\ncs.db `
  --source training-courses `
  --source job-base `
  --apply `
  --state-dir .state\ncs-api-refresh `
  --out reports\ncs_api_refresh_evidence.json
```

If any major-code page fails or completion cannot be proven, the command exits
without a publishable `prepared_output`; the original DB remains byte-for-byte
unchanged. Absence in an API response is never treated as deletion. Qualification
and NCS006 collection remain outside this automatic path because they require
the existing retry-hygiene, coverage-plan, and operator-ready gates.

## Verified baseline promotion

A prepared DB does not become the next comparison baseline merely because a
local build succeeded. Baseline promotion requires all three evidence files:

1. a successful, non-blocked ontology apply report;
2. a successful, non-dry compact snapshot publish report for the exact same DB;
3. a successful remote MCP transport verification report.

After those checks, the promotion command stores an immutable versioned
baseline, a lineage sidecar, and an atomic `current.json` pointer under the
persistent state directory:

```powershell
python scripts\promote_ncs_refresh_baseline.py `
  --refresh-report reports\ncs_ontology_refresh_apply.json `
  --publish-report reports\vercel_snapshot_publish_report.json `
  --remote-verification reports\remote_mcp_transport_verify.json `
  --state-dir C:\ncs_mcp_state\ncs-ontology-refresh `
  --out reports\baseline_promotion_report.json
```

A failed build, deployment, or remote verification leaves `current.json`
untouched. Versioned baselines are not deleted automatically; retention is an
explicit operator task after backup and rollback requirements are satisfied.

## Automatic refresh and Vercel release shape

`.github/workflows/vercel-snapshot-release.yml` implements the complete guarded
sequence on a self-hosted Windows runner:

```text
HTTPS ncs.db download + optional SHA-256 check
  -> optional all-major supplemental API refresh on a working copy
  -> source-diff plan + change-aware ontology preparation
  -> compact ZIP/manifest in a temporary tracked-code deploy root
  -> Vercel staged deployment
  -> exact deployment MCP verification
  -> production promotion + public MCP verification
  -> verified baseline promotion
```

The workflow accepts only HTTPS source URLs. A manual override host must match
the configured source host or `NCS_SOURCE_DB_ALLOWED_HOSTS`. Its persistent
state directory is outside the checkout (`NCS_REFRESH_STATE_DIR`, or a
self-hosted runner workspace sibling by default). `api_refresh_mode=auto` uses
whichever safe supplemental API credentials are configured; `require` demands
both; `skip` performs no network collection.

The deterministic release path is deliberately not AI. It needs reproducible
file transforms, source identity checks, rollback boundaries, and explicit
promotion evidence. AI can generate HR outputs through the MCP tools, but it
does not decide how deployment data is rebuilt or approved.

Because the canonical source DB is currently about 12.6 GB, the self-hosted
runner needs space for the downloaded source, API working copy, ontology
working copy, compact build, and persistent versioned baseline. Vercel remains
lightweight because only the compact ZIP and manifest are deployed.

The runtime validates the manifest/archive before materializing the SQLite file
under `/tmp`, then opens that path read-only. It does not use `NCS_DB_URL` in
the standard deployment flow.

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
