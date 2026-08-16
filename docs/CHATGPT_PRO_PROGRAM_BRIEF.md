# Program Brief

This program is now an NCS training ontology MCP.

## Objective

Given an NCS task query, return training-course recommendations with:

- NCS competency unit;
- competency element;
- performance criterion;
- source and gap KSA concepts;
- task transition evidence;
- NCS training course;
- saved recommendation evidence chain.

## Active Data Sources

- NCS Excel DB.
- NCS reference APIs.
- NCS training course API `ncsTrainingCource/openapi18`.

## Removed From Active Scope

- SQF API and SQF reports.
- NCS study-module API.
- SQF-NCS mapping and SQF-based gap analysis.

## Main Commands

```powershell
python scripts\ncs_harness.py collect-training-courses --all-majors
python scripts\ncs_harness.py preprocess-ncs-ontology --atomic-ksa --task-ksa-relations --task-similarity --training-course-links
python scripts\ncs_harness.py recommend-training-for-task --query "인력채용"
```
