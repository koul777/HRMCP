# NCS Training Ontology Architecture

## Purpose

The system turns NCS source data into a relationship-centered SQLite graph for HR training recommendations. It is not a PDF text searcher and it no longer uses SQF or NCS study modules in the active recommendation surface.

## Active Sources

- `data/raw/ncs_info_network_db_2026_02.xlsx`: source NCS hierarchy, competency units, elements, performance criteria, and KSA rows.
- NCS reference APIs: unit/classification/element enrichment.
- NCS training course API `ncsTrainingCource/openapi18`: official training-course rows linked to NCS unit codes.

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

Recommendation audit layer:

- `education_recommendation_runs`
- `education_recommendation_items`
- `education_recommendation_evidence`

Legacy compatibility tables for SQF and NCS study modules may still exist in SQLite so old generated DBs remain readable, but active MCP tools and harness workflows do not depend on them.

## Processing Pipeline

```text
preprocess_excel.py
  -> load normalized NCS source tables

collect_api.py
  -> enrich NCS units/classifications/elements

training_course_api.py
  -> collect openapi18 rows
  -> exact ncs_cl_cd to competency_units links

preprocess-ncs-ontology
  -> seed ontology concepts from KSA without overwriting raw KSA
  -> split atomic KSA
  -> build task KSA concept relations
  -> build task similarity links
  -> build training-course concept links

training_recommendation.py
  -> resolve source task
  -> gather source and gap KSA concepts
  -> rank training courses
  -> save evidence chain
```

## Invariants

- `ksa_items.ksa_text_raw` is never modified.
- Raw KSA text is not copied into `ontology_concepts.definition`.
- Missing definitions remain `definition_status='missing'`.
- Human-authored definitions use `definition_status='defined'` and `review_status='human_reviewed'`.
- Training recommendations always return NCS unit, element, performance criterion, KSA concept, training-course, and audit evidence when available.
- Recommendation output is guidance, not an official recognition or qualification decision.

## Validation

```powershell
python -m unittest discover -s tests -v
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py smoke
python scripts\ncs_harness.py ontology validate
```
