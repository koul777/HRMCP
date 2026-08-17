# NCS Training Ontology MCP PRD

## Goal

Build an MCP server that recommends NCS training courses from task-level KSA evidence.

The product is not a general document searcher. It exposes a structured graph:

```text
NCS Classification
  -> Competency Unit
  -> Competency Element
  -> Performance Criterion as Task
  -> Atomic KSA Concept
  -> Training Course
  -> Evidence Chain
```

## Users

- HR staff designing training plans.
- Education planners mapping NCS tasks to training courses.
- Analysts reviewing KSA gaps and upskilling/reskilling paths.

## Product Packaging And Deployment Strategy

The near-term product is an internal NCS-centered HR training-planning and
evidence MCP. It is not a public qualification-recognition service, legal
eligibility service, or automated HR approval authority.

Supported packages:

- Desktop analyst mode: local STDIO or local HTTP for a single HR analyst or
  education planner working against a prepared SQLite database.
- Internal team service mode: private HTTP MCP hosted by IT/platform staff for
  a controlled HR or education-planning team.
- Containerized internal deployment: Docker-based internal hosting with an
  attached DB volume and environment-managed service credentials.
- Batch refresh mode: guarded collection and preprocessing jobs refresh the DB;
  recommendation serving remains separated from broad API collection.

Credential ownership:

- NCS API service keys are organization-owned operational credentials.
- Platform/ops or a designated data operator owns collection credentials and
  refresh jobs.
- Normal HR planning users consume DB-backed recommendation and review surfaces
  without direct access to raw API keys.
- Health and readiness surfaces may expose only key-presence booleans, never
  secret values.

User workflow:

- The HR user enters current role, target role or task, and optional level,
  time, method, or facility constraints.
- The system resolves NCS scope, returns `query_route`, and builds task/KSA
  evidence before recommending courses.
- The user reviews `recommended_path`, `training_system_matrix`, evidence
  highlights, and human-review flags as planning guidance.
- A human reviewer or organizational process decides required/optional status,
  facility fit, and whether to adopt a plan outside the MCP.

Operational responsibilities:

- Product owner: scope, wording, and non-claim policy.
- Platform/ops: deployment, access control, secrets, health/readiness, backups,
  and DB volume management.
- Data operator: guarded API collection, retry/rate-limit handling, and refresh
  cadence.
- HR reviewer/planner: required/optional judgment, plan acceptance, and
  downstream HRD approval decisions.
- Engineering/QA: tool contract checks, lint/smoke/tests, dashboard verification,
  and evidence-surface integrity.

Current release acceptance:

- Each supported deployment mode names the transport, DB location pattern, key
  owner, and whether collection is allowed there.
- Shared internal deployment is supported; public anonymous hosting and
  cross-company multi-tenant SaaS are deferred until auth, tenancy, and audit
  requirements are defined.
- Normal planning usage must not require exposing API keys to HR end users.
- Product wording must state that recommendations are NCS-based training
  guidance, not official qualification, legal eligibility, or hiring decisions.
- Human review responsibility remains explicit for required/optional decisions,
  approval, and organizational adoption.

Operational enforcement details live in `docs/MCP_RELEASE_CHECKLIST.md`; this
PRD section defines the product contract those checks support. Productization,
buyer/user packaging, and commercial boundary details live in
`docs/AIHR_PRODUCTIZATION_STRATEGY.md`. Day-to-day deployment and rollback
steps live in `docs/AIHR_DEPLOYMENT_RUNBOOK.md`.

## Requirements

- Preserve NCS source rows and raw KSA text.
- Split KSA into atomic candidate items.
- Link tasks to KSA concepts.
- Calculate task similarity from KSA concept overlap.
- Collect NCS training course API rows for all major classifications.
- Link training courses to NCS units and KSA concepts.
- Recommend training with task, KSA, unit, and evidence details.
- Save recommendation audit chains in `education_recommendation_*`.
- Keep the converted 2026 NCS HRD guide as `docs/reference/ncs_hrd_guide_codex_readable.md`
  and regenerate `docs/reference/ncs_hrd_guide_reference.index.json` so
  education-plan prompts are evaluated against the same framework reference.

## Out Of Scope

- SQF collection, SQF-NCS mapping, SQF report evidence, and SQF-based recommendation.
- NCS study-module API recommendation.
- Official qualification or legal eligibility decisions.

## Success Criteria

- `recommend_training_for_task` returns relevant training courses for an NCS task query.
- Every recommendation includes source task, KSA concept evidence, and training-course source IDs.
- `plan_ncs_education_path` can answer guide-style prompts with `query_route`,
  `recommended_path`, `training_system_matrix`, and `training_system_guide_trace`.
- Tests, lint, smoke, and ontology validation pass.
