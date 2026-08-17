# NCS Training Ontology Architecture

## Purpose

The system turns NCS source data into a relationship-centered SQLite graph for HR training recommendations. It is not a PDF text searcher and it no longer uses SQF or NCS study modules in the active recommendation surface.

## Active Sources

- `data/raw/ncs_info_network_db_2026_02.xlsx`: source NCS hierarchy, competency units, elements, performance criteria, and KSA rows.
- NCS reference APIs: unit/classification/element enrichment.
- NCS training course API `ncsTrainingCource/openapi18`: official training-course rows linked to NCS unit codes.
- NCS career-development-path CSV: supporting evidence for level and transition fit.
- NCS unit qualification-item API: supporting evidence for related qualifications.
- NCS job-base competency API: supporting evidence for common and missing foundational capabilities.
- The 2026 NCS HR practical guide for education/training system building is a workflow and validation rubric, not a source of scored training-course facts. Its project copy and preprocessed reference index live under `docs/reference/`; see `docs/NCS_HRD_GUIDE_REFERENCE.md`.

## Core Tables

NCS source layer:

- `classifications`
- `competency_units`
- `competency_elements`
- `performance_criteria`
- `ksa_items`
- `element_criteria_ksa_links`

KSA ontology layer:

- `ksa_atomic_items`
- `ontology_concepts`
- `ontology_concept_aliases`
- `ksa_concept_links`
- `ksa_atomic_concept_links`
- `criteria_concept_links`
- `task_ksa_concept_relations`
- `task_similarity_links`

Training layer:

- `ncs_training_courses`
- `ncs_training_course_unit_links`
- `ncs_training_course_concept_links`
- `ncs_training_course_element_links`
- `training_goal_concept_links`
- `training_delivery_relations`

Supporting evidence layer:

- `ncs_career_paths`
- `ncs_qualification_items`
- `ncs_unit_qualification_links`
- `ncs_job_base_competencies`
- `ncs_job_base_factors`
- `ncs_unit_job_base_links`

Recommendation audit layer:

- `education_recommendation_runs`
- `education_recommendation_items`
- `education_recommendation_evidence`

Legacy compatibility tables for SQF and NCS study modules may still exist in SQLite so old generated DBs remain readable, but active MCP tools and harness workflows do not depend on them.

## SQLite Operational Boundary

The generated `data/processed/ncs.db` is a local prepared knowledge graph, not
an embedded application asset. Current full builds can be roughly 12 GB, so the
supported release posture is read-heavy internal use with tightly controlled
batch writes.

Supported with SQLite:

- Single-user desktop analyst mode.
- Internal team service mode when MCP planning requests run read-heavy/no-save
  and broad collection jobs are scheduled separately.
- One guarded writer at a time for API collection, preprocessing, or review
  imports.
- Read-only diagnostics and dashboard checks that open the DB with `mode=ro`
  where available.

Not treated as SQLite-ready release scope:

- Public or multi-tenant hosting.
- Concurrent broad collectors or review writers against the same live DB.
- Real-time API collection overlapping shared planner traffic.
- Large backup/restore windows that cannot meet the deployment recovery target.

Operational rules:

- Mount the DB as a volume; do not bake it into Docker images.
- Keep `*.db-wal`, `*.db-shm`, and `*.db-journal` sidecars with the base DB
  until DB users are stopped and a checkpoint/backup plan has run.
- Separate serving from broad collection; release demos and dashboard live plans
  must use no-save/read-only planner paths.
- Capture DB size, sidecar state, and queue/API cooldown state in release
  evidence before shared deployment.

Migration triggers:

- More than one regular writer is required.
- SQLite busy timeouts or write-lock waits appear during normal shared use.
- Planner queries require large joins or indexes that push response time beyond
  the product SLO.
- Backup, restore, or file transfer of the single DB no longer fits the
  operational recovery window.

When those triggers appear, split serving from ingestion by moving the active
planner store to a server-grade database or a replicated read model while
keeping raw-source preservation and ontology invariants unchanged.

## Processing Pipeline

```text
preprocess_excel.py
  -> load normalized NCS source tables

collect_api.py
  -> enrich NCS units/classifications/elements

training_course_api.py
  -> collect openapi18 rows
  -> exact ncs_cl_cd to competency_units links
  -> preserve goal, hour, method, facility, and level fields

preprocess-ncs-ontology
  -> seed ontology concepts from KSA without overwriting raw KSA
  -> split atomic KSA
  -> build task KSA concept relations
  -> build task similarity links
  -> build training-course unit, element, concept, goal, and delivery links

collect-job-base / collect-qualification-items
  -> collect supporting evidence without making official eligibility decisions

query_router.py
  -> map natural-language requests to NCS task, transition, evidence, review, or education-system scenarios
  -> return query_route contracts for MCP facade tools

training_recommendation.py
  -> resolve source task
  -> gather source and gap KSA concepts
  -> rank training courses
  -> build recommended_path and training_system_matrix
  -> save evidence chain when requested

preprocess-hrd-guide-reference
  -> copy docs/reference/ncs_hrd_guide_codex_readable.md into the project
  -> generate docs/reference/ncs_hrd_guide_reference.index.json
  -> generate docs/reference/ncs_hrd_guide_reference.md
  -> generate docs/reference/ncs_hrd_guide_reference.chunks.jsonl
  -> preserve the guide as framework_reference validation material only
```

## AI-HR Planner Output Contract

The active user-facing facade is `plan_ncs_education_path`, backed by
training-transition and task-training recommendation flows. Its output must be
usable as a first draft of an education/training system, not just a search
result list.

Required planner surfaces:

- `query_route`: selected tool, scenario, expected chain, route contract, and route fingerprint.
- `recommended_path`: ordered groups such as scope confirmation, core gap training, supporting or adjacent training, and delivery-stage constraints.
- `training_system_matrix`: course rows grouped by job scope, target level band, education type, required/optional basis, delivery operation, planner grouping, task/KSA basis, facility constraint fit, human review state, and course fit.
- `training_system_guide_trace`: proof that the 2026 guide rubric was checked for job scope, task/KSA, course linkage, required/optional reasoning, level/delivery, and human review.

The guide trace is a validation rubric. It does not directly raise scores and
does not turn sample guide rows into source data.

## Invariants

- `ksa_items.ksa_text_raw` is never modified.
- Raw KSA text is not copied into `ontology_concepts.definition`.
- Missing definitions remain `definition_status='missing'`.
- Human-authored definitions use `definition_status='defined'` and `review_status='human_reviewed'`.
- Training recommendations always return NCS unit, element, performance criterion, KSA concept, training-course, and audit evidence when available.
- Recommendation output is guidance, not an official recognition or qualification decision.
- Automated processing must not write `human_reviewed`, `accepted`, or `reviewed` states without an explicit human decision.
- Facility evidence can be `unknown` or `not_requested`; those states should trigger review context rather than silent failure.

## Validation

```powershell
python -m unittest discover -s tests -v
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py smoke
python scripts\ncs_harness.py ontology validate
python scripts\ncs_harness.py run-aihr-plan-demo --current-query "노무관리" --target-query "인사기획" --out reports\aihr_plan_demo_20260617.json --html-out reports\aihr_plan_demo_20260617.html
python scripts\ncs_harness.py verify-aihr-dashboard --base-url http://127.0.0.1:8765 --out reports\aihr_dashboard_surface_verification_20260617.json --markdown-out reports\aihr_dashboard_surface_verification_20260617.md
```
