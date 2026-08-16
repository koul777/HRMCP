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

## Requirements

- Preserve NCS source rows and raw KSA text.
- Split KSA into atomic candidate items.
- Link tasks to KSA concepts.
- Calculate task similarity from KSA concept overlap.
- Collect NCS training course API rows for all major classifications.
- Link training courses to NCS units and KSA concepts.
- Recommend training with task, KSA, unit, and evidence details.
- Save recommendation audit chains in `education_recommendation_*`.

## Out Of Scope

- SQF collection, SQF-NCS mapping, SQF report evidence, and SQF-based recommendation.
- NCS study-module API recommendation.
- Official qualification or legal eligibility decisions.

## Success Criteria

- `recommend_training_for_task` returns relevant training courses for an NCS task query.
- Every recommendation includes source task, KSA concept evidence, and training-course source IDs.
- Tests, lint, smoke, and ontology validation pass.
