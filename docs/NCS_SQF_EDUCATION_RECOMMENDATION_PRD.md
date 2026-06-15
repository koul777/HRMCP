# NCS-SQF Education Recommendation MCP MVP

This document captures the implementation baseline from
`NCS_SQF_온톨로지_교육추천_MCP_PRD_v1.0.docx`.

## MVP Goal

For the `02` NCS major domain and the SQF management support / HR scope, the MCP
server recommends education with an auditable evidence chain. A recommendation is
not a keyword result list and is not an official recognition decision.

Each recommendation should connect, when available:

- SQF direct evidence: education/training, qualification, career, license, remarks.
- SQF report evidence: document chunk candidate with document title, asset, page, chunk id.
- Trusted SQF-NCS mapping: only `accepted`, `reviewed`, or `human_reviewed`.
- NCS supplement evidence: competency units, performance criteria, KSA.
- KSA ontology concepts: representative concepts, definition status, review status.
- NCS learning modules: cached rows from openapi21.

## MVP Interfaces

- `collect-study-modules`: collect and upsert NCS learning modules into SQLite.
- `search_learning_modules`: search cached learning modules.
- `get_learning_module`: return a module with unit/concept links.
- `recommend_education_for_duty`: generate PRD-style recommendation JSON and save audit rows.
- `explain_education_recommendation`: replay a saved recommendation item evidence chain.

## v1 Operational Hardening

The current v1 hardening layer adds:

- Common MCP `ok/data/error/audit` envelopes for the SQF mapping, gap, education
  recommendation, learning module, SQF document, precision-match, and ontology
  evidence tools while preserving legacy top-level fields.
- `get_learning_path_for_sqf_job` for staged SQF duty-level learning paths.
- `search_ontology_concepts` and `get_concept_evidence` for concept-level traceability.
- `review_sqf_ncs_match` and `review_ontology_concept` for human review with
  `review_audit_log` entries.
- Dashboard recommendation audit views backed by
  `education_recommendation_runs`, `education_recommendation_items`, and
  `education_recommendation_evidence`.
- Evaluation metrics for recommendation run count, item/evidence coverage,
  candidate leakage, source evidence rates, and a pending human
  `Precision@5` baseline marker.

## Invariants

- Candidate, rejected, low-score, and related-only SQF-NCS mappings are excluded from trusted recommendations.
- SQF document chunks are exposed as summarized candidate evidence, not full source text.
- KSA raw text and Excel source fields are not modified by recommendation workflows.
- Recommendation run/item/evidence rows store source ids, chunk ids, match ids, unit codes, concept ids, and learning module sequence values.

## Verification

Run the normal repository checks plus a learning-module collection check:

```powershell
python -m unittest discover -s tests -v
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py smoke
python scripts\ncs_harness.py ontology validate
python scripts\ncs_harness.py collect-study-modules --major-code 02 --num-of-rows 200
```
