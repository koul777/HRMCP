# AI-HR Productization And Deployment Strategy

## Positioning

AI-HR is an internal, NCS-centered education-planning and evidence assistant for
HRD teams. It turns the NCS source DB and supplemental APIs into an auditable
planning graph:

```text
Job or transition goal
  -> NCS scope
  -> task and performance criteria
  -> KSA gap and transfer evidence
  -> training course
  -> qualification and job-base evidence
  -> human review and adoption decision
```

It is not a public qualification-recognition service, legal eligibility service,
hiring decision engine, or automated HR approval system.

## Users And Buyers

Primary users:

- HR staff designing job-based training plans.
- Education planners mapping NCS tasks to internal or external courses.
- HR analysts reviewing upskilling and reskilling evidence.

Operational buyers or sponsors:

- HRD or talent-development lead.
- HR operations lead responsible for job and competency frameworks.
- Internal platform or data team that can host MCP services and manage API keys.

Review stakeholders:

- HR reviewers who decide required or optional course status.
- Job-domain experts who confirm task and KSA fit.
- Compliance or policy owners when wording could imply official recognition.

## Packaging

The near-term product should be packaged in three stages.

1. Desktop analyst pilot

   - Runs locally through STDIO or localhost HTTP.
   - Uses a prepared SQLite DB and generated reports.
   - Best for demos, HR analyst evaluation, and evidence review.
   - API collection is operator-only and not exposed to normal HR users.

2. Internal team service

   - Runs as a private HTTP MCP and dashboard behind internal access controls.
   - Uses a mounted DB volume.
   - Supports shared HRD planning workflows, live route checks, queue status,
     review seedpacks, and dashboard verification.
   - API keys are owned by platform/ops or a designated data operator.

3. Managed internal product

   - Adds a stable deployment runbook, backup and restore procedure, review
     import process, and release-readiness gate.
   - Separates serving from broad collection jobs.
   - Treats `recommended_path`, `training_system_matrix`, review status, and
     queue artifacts as product surfaces rather than one-off reports.

Public anonymous hosting and cross-company multi-tenant SaaS are deferred until
auth, tenant isolation, audit logging, customer data boundaries, billing, and
upstream API terms are explicitly designed.

## Business Model Hypotheses

Near-term commercial shape:

- Pilot or consulting package: NCS DB build, API linkage audit, and HRD use-case
  demonstration for one organization.
- Internal deployment package: private MCP/dashboard setup, DB refresh runbook,
  and release-readiness checklist.
- Review enablement package: ontology definition seedpacks, training-link review
  packets, and reviewer workflow support.
- Maintenance package: periodic API refresh, quality reports, regression checks,
  and evidence-surface updates.

What should not be sold as-is:

- Official qualification recognition.
- Legal eligibility judgment.
- Automated hiring, promotion, or mandatory-training approval.
- Public API-key proxy service.

The defensible value is not "AI recommends a course." The value is that HR can
trace why a course is recommended from NCS task/KSA evidence and keep the final
decision under human review.

## Deployment Responsibility Model

| Area | Owner | Notes |
| --- | --- | --- |
| Product scope and wording | Product owner | Blocks official-recognition or legal-eligibility claims. |
| MCP hosting and access | Platform/ops | Owns private HTTP/STDIO registration and network exposure. |
| API keys | Platform/ops or data operator | HR end users must not receive raw service keys. |
| API collection jobs | Data operator | Uses guarded retries, rate-limit checkpoints, and broad-scope collection policy. |
| DB volume and backups | Platform/ops | SQLite is acceptable for read-heavy internal use; sidecars must be handled. |
| Recommendation evidence QA | Engineering/QA | Runs tests, lint, smoke, dashboard verification, and release-readiness reports. |
| Human review decisions | HR reviewer/domain expert | Decides required/optional status, definition approval, and adoption. |

Operational deployment, rollback, and release sequencing details live in
`docs/AIHR_DEPLOYMENT_RUNBOOK.md`.

## Release Gates For Product Claims

Before describing the system as deployable for HR planners:

- AI-HR demo and live planner expose `query_route.tool=plan_ncs_education_path`.
- Public demo artifacts include `recommended_path`, `training_system_matrix`,
  and `training_system_guide_trace`.
- Queue status has no unsafe automated human-decision item.
- Review seedpacks remain export-only until a separate human decision apply
  step exists with reviewer id, rationale, timestamp, and source packet.
- Qualification coverage gap is either resolved or explicitly disclosed as a
  remaining evidence-quality limitation.
- Broad API collection is guarded by retry/rate-limit policy.
- SQLite operating boundary is accepted for the deployment mode, or a server DB
  migration plan is created.

## Go-To-Market Message

Use this framing for public posts or stakeholder demos:

```text
We are rebuilding NCS from a search DB into an explainable HR competency graph.
```

The product story should emphasize:

- Evidence traceability from job, task, and KSA to training.
- Human-reviewed education planning, not automated approval.
- Data completeness by linking NCS source DB with standard, training, job-base,
  and qualification APIs.
- Practical HRD outputs: recommended paths, training-system matrix, review
  seedpacks, and annual operation-plan seeds.
