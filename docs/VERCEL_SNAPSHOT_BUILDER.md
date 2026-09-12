# Vercel compact snapshot Builder

The Windows Data Builder is the single operator entry point for the entire
production lifecycle:

```powershell
.\run_ncs_builder.bat
```

Production DB/API refresh, ontology rebuild, compact package creation, Vercel
release, remote verification, and baseline promotion must run as stages of the
same selected Builder version. Packaging is an internal, version-bound Builder
stage: it rechecks the canonical source hash and creates the verified compact
DB, ZIP, and manifest inside that version's `release` directory.
`scripts/publish_vercel_snapshot.py` is retained only for dry-run diagnostics
and legacy recovery tests; its CLI cannot perform a non-dry publication. It is
not a second operator runbook.

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

## Non-operational implementation inspection

`scripts/build_vercel_snapshot.py` remains the low-level deterministic component
for Builder-owned custom output paths. It refuses to replace an output. The
only standalone example retained here is a no-write inspection of resolved
paths and exact argument arrays; it is not a package or release instruction:

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

The selected Builder version invokes the change-aware ontology component after
source-delta review. Read-only planning may be used for implementation
diagnostics, but candidate creation is performed only through the Builder UI.
The internal component creates a separate prepared database and never writes to
the supplied DB or promoted baseline.

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

Training-course and job-base APIs are refreshed only by the selected Builder
version's API stage. The internal component discovers all NCS major codes from
the DB, creates a consistent SQLite online backup including committed WAL
frames, and calls the APIs only against that working copy.

If any major-code page fails or completion cannot be proven, the command exits
without a publishable `prepared_output`; the original DB remains byte-for-byte
unchanged. Absence in an API response is never treated as deletion. Qualification
and NCS006 collection remain outside this automatic path because they require
the existing retry-hygiene, coverage-plan, and operator-ready gates.

## Verified baseline promotion

A prepared DB does not become the next comparison baseline merely because a
local build succeeded. Baseline promotion requires these evidence files:

1. a successful, non-blocked ontology apply report;
2. a successful, non-dry compact snapshot publish report for the exact same DB;
3. a successful production MCP transport verification report;
4. in the automated staged-release path, the successful exact staged-deployment
   MCP verification report as well.

After those checks, the Builder's promotion stage stores an immutable versioned
baseline, a lineage sidecar, and an atomic `current.json` pointer under the
persistent state directory. `scripts/promote_ncs_refresh_baseline.py` is the
internal implementation boundary, not an operator bypass.

A failed build, deployment, or remote verification leaves `current.json`
untouched. Versioned baselines are not deleted automatically; retention is an
explicit operator task after backup and rollback requirements are satisfied.

## Builder-only refresh and Vercel release shape

The Windows Data Builder is the single owner of the data-to-production path.
It operates on the local canonical `data/processed/ncs.db`, creates a versioned
working copy, refreshes the explicitly selected supplemental APIs, prepares the
change-aware ontology candidate, builds the compact snapshot, verifies it, and
then performs the guarded Vercel release from the selected Builder version.

```text
run_ncs_builder.bat
  -> source delta + ontology candidate
  -> optional all-major supplemental API refresh
  -> compact ZIP/manifest build and verification
  -> exact Vercel deployment and MCP verification
  -> verified baseline promotion
```

The former scheduled GitHub Actions snapshot workflow and its self-hosted
runner are retired. GitHub Actions remains available for CI tests only; it is
not a second data refresh or deployment authority. This prevents the local
Builder's versioned `publisher_source` from diverging from an unrelated
downloaded database.

The deterministic release path is deliberately not AI. It needs reproducible
file transforms, source identity checks, rollback boundaries, and explicit
promotion evidence. AI can generate HR outputs through the MCP tools, but it
does not decide how deployment data is rebuilt or approved.

Because the canonical source DB is currently about 12.6 GB, the Windows Builder
host needs space for the selected source, API working copy, ontology
working copy, compact build, and persistent versioned baseline. Vercel remains
lightweight because only the compact ZIP and manifest are deployed.

The runtime validates the manifest/archive before materializing the SQLite file
under `/tmp`, then opens that path read-only. It does not use `NCS_DB_URL` in
the standard deployment flow.

## Guarded release internals and recovery

Direct preview or production deployment is not an operator procedure. It would
bypass the selected Builder version, source identity, durable transaction, and
rollback fencing. The Builder release uses this fixed internal sequence:

1. Copy only Git-tracked deployment source into the isolated version stage and
   fail before building if a required local import is absent from that stage.
2. Run `vercel build --prod --yes`, then verify the exact
   `.vercel/output/functions/python.func` with
   `verify_vercel_compact_package.py`.
3. Record the current production deployment, upload that unchanged output with
   `vercel deploy --prebuilt --prod --skip-domain --yes`, and verify the unique
   deployment's health, readiness, MCP contract, and exact build identity.
4. Reconfirm that production did not change concurrently, promote only the
   verified unique URL, and repeat the health/readiness/MCP/build check through
   the production URL.
5. If the post-promotion check fails, explicitly roll back to the recorded
   previous production URL and reconfirm the restored target. Baseline promotion
   remains blocked regardless of rollback outcome.

The release report contains an atomic `deployment_transaction` checkpoint. It
records the original known-good production deployment before staging, records
promotion intent before calling the CLI, and records rollback intent before a
rollback. If the process stops after promotion intent, the next invocation
first inspects production and restores the original target when the staged URL
is live. Immediately before rollback it inspects production again and runs the
rollback only when the exact expected staged deployment ID is still current;
an already restored known-good target is accepted without mutation, while a
third deployment is recorded as divergence and left untouched. A
`rollback_pending`, `rollback_unconfirmed`,
`promotion_outcome_unconfirmed`, or `production_diverged` state blocks automatic
retry; an operator must reconcile Vercel production state before any new
deployment attempt. Retries never replace the original known-good target with
an unverified promoted deployment. Legacy failed reports that record promotion
or an unconfirmed rollback without a durable transaction are migrated on first
inspection to a persistent `promotion_outcome_unconfirmed` transaction. The
migration retains the original known-good deployment identity when legacy
evidence contains it and is flushed before attempt fields are cleared, so the
first retry, later retries, and retries after a Builder process restart all
remain blocked without deploy, promote, or rollback calls. Report checkpoints
flush file contents before atomic replace; directory metadata sync is applied
where the operating system supports it.

The Vercel CLI inspection followed by rollback is not a remote atomic
compare-and-swap and does not provide a fencing token. Run exactly one Builder
deployment authority per Vercel project. The surrounding scheduler must enforce
a singleton lease (and a fencing mechanism when multiple hosts can contend)
before invoking the Builder; the local atomic release report is durable recovery
evidence, not a distributed lock. If overlapping authorities may have run, stop
automatic deployment and reconcile production explicitly.

The Builder never fills a missing import from arbitrary untracked files. For
example, if `ncs_mcp.search.core` imports `ncs_mcp.search.normalization` but the
corresponding source file is not tracked and copied, the release stops with a
source-package error. `git.deploymentEnabled=false` prevents Git pushes from
deploying a commit that lacks the ignored ZIP. There is no scheduled/Git
deployment authority; release only through `run_ncs_builder.bat` after the
Publisher stage has completed.
