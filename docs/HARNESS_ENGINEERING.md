# Harness Engineering

The active harness supports NCS source loading, NCS API enrichment, KSA/task ontology preprocessing, NCS training-course collection, and task-based training recommendation.

## Required Checks

```powershell
$env:PYTHONPATH="C:\workspace\NCS_MCP\src"
python scripts\ncs_harness.py inspect
python scripts\ncs_harness.py lint
python scripts\ncs_harness.py smoke
python -m unittest discover -s tests -v
python scripts\ncs_harness.py quality-gates --out reports\quality_gates.json --markdown-out reports\quality_gates.md
```

`quality-gates` is a read-only health check. It opens the SQLite DB in read-only
mode, does not initialize or migrate schema, and returns exit code `1` only when
at least one gate has `status: "fail"`. A warning-only report is still a valid
run and should be used as the next data-quality backlog.

Use the transition gate only when reviewed scenarios exist:

```powershell
python scripts\ncs_harness.py quality-gates --include-transition-eval --transition-limit 5 --transition-scenario-limit 20 --out reports\quality_gates_with_transition.json
```

Candidate or auto-generated transition scenarios are not hard-gated. If no
`human_reviewed`, `reviewed`, or `accepted` scenarios exist, the command reports
a warning instead of failing the run.
`--transition-limit` controls recommendations per scenario, while
`--transition-scenario-limit` caps how many trusted scenarios the quality gate
evaluates.

## Training-Course Collection

Collect one major classification:

```powershell
python scripts\ncs_harness.py collect-training-courses --major-code 02 --num-of-rows 500
```

Collect all major classifications discovered from `classifications`:

```powershell
python scripts\ncs_harness.py collect-training-courses --all-majors --num-of-rows 500
```

The collector stores rows in `ncs_training_courses` and exact NCS unit-code links in `ncs_training_course_unit_links`.
Storage commands require an explicit scope: pass `--all-majors` for operating
collection or `--major-code` for a scoped debug refresh. The same rule applies to
`collect-job-base` and legacy `collect-study-modules`.

Qualification coverage should be checked before relying on qualification evidence
in recommendations:

```powershell
python scripts\ncs_harness.py qualification-summary --limit 10
```

The summary includes total/attempted/unattempted unit counts, collection coverage,
status counts, and error concentration by NCS major code.

Before retrying qualification API errors, generate a read-only hygiene report.
This does not call the API or mutate retry metadata; it classifies cached
errors, shows missing `last_error_type` / `attempt_count` / `next_retry_at`
metadata, and warns when broad retries are likely to hit rate limits again.

```powershell
python scripts\ncs_harness.py qualification-retry-hygiene --limit 50 --out reports\qualification_retry_hygiene.json --markdown-out reports\qualification_retry_hygiene.md
```

Only use `retry-qualification-errors` after inspecting that report, preferably
with a small `--limit-units`, a request delay, and low retry count while
rate-limit errors dominate.

Use the rate-limit circuit breaker on every broad qualification retry or
all-unit collection batch:

```powershell
python scripts\ncs_harness.py retry-qualification-errors --limit-units 50 --num-of-rows 50 --max-pages 1 --request-delay 2 --max-retries 1 --retry-backoff-seconds 30 --stop-after-rate-limit-errors 3 --report-path reports\qualification_error_report.md
python scripts\ncs_harness.py collect-qualification-items --all-units --limit-units 100 --num-of-rows 50 --max-pages 1 --request-delay 2 --max-retries 1 --retry-backoff-seconds 30 --stop-after-rate-limit-errors 3
```

If the command output includes `stopped_early=true` or
`stop_reason=rate_limited`, stop the collection wave, keep the generated error
report, and wait for the API retry window before increasing `--limit-units`.
Do not use `--include-not-due` or `--refresh` for routine recovery runs.
`--refresh` is only for an explicit re-collection decision after reviewing the
cached status table and API conditions.

## Ontology Preprocessing

```powershell
python scripts\ncs_harness.py preprocess-ncs-ontology --atomic-ksa --task-ksa-relations --task-similarity --training-course-links
```

This command:

- preserves raw KSA in `ksa_items`;
- creates atomic KSA candidates in `ksa_atomic_items`;
- links KSA to `ontology_concepts`;
- creates task KSA relations and task similarity;
- links training courses to KSA concepts in `ncs_training_course_concept_links`.

## Recommendation Smoke

```powershell
python scripts\ncs_harness.py recommend-training-for-task --query "인력채용" --limit 5 --compact --no-save
```

Recommendation output must include NCS task context, KSA concepts, training courses, and evidence. It must not depend on SQF or NCS study modules.

Recommendation responses include both the full `recommendations` list and compact
`recommendation_groups`:

- `primary`: direct target-scope or strong training-goal/element evidence.
- `supplemental`: useful support courses for missing or adjacent competencies.
- `adjacent`: low-confidence nearby courses for reference only.

Each grouped item includes compact decision fields for UI and operator review:
`evidence_strength`, `evidence_strength_summary`, `evidence_highlights`,
`delivery`, `coverage_counts`, `coverage_breakdown`, `coverage_summary`,
`score_component_highlights`, `direct_unit_evidence`, and
`source_element_covered`.

Compact cards should expose small named evidence samples, not only counts.
`evidence_highlights` keeps top KSA, ability-element, qualification, and
job-base labels so a user can see why the course was returned without loading
the raw full `recommendations` payload. `why_recommended` summarizes the most
important named evidence and match-basis details in a few lines. When a query is short or broad,
`input_quality.candidate_queries` provides structured candidate follow-up
queries.

For user-facing transition checks, prefer compact output:

```powershell
python scripts\ncs_harness.py recommend-training-transition --current-query "노무관리" --target-query "인사기획" --limit 5 --compact --no-save
```

The compact view keeps scope interpretation, KSA gaps, job-base/qualification
signals, and course cards, while omitting raw `recommendations`, `audit`, and
`source_payload` debug details. Low-evidence supplemental courses are displayed
as `adjacent_reference` so operators do not read weak evidence as a primary
recommendation.

Invalid task locator input for `recommend-training-for-task` and invalid
`review-triage` inputs print structured JSON errors and exit non-zero. Treat
those as failed automation steps.

Human-review queue commands support dry runs for planning without mutating
`quality_issues`:

```powershell
python scripts\ncs_harness.py prepare-hr-review-queue --concept-limit 300 --goal-link-limit 300 --dry-run
python scripts\ncs_harness.py prepare-ontology-review-queue --concept-limit 500 --goal-link-limit 500 --relation-limit 500 --dry-run
```

Use `review-priority` to inspect the highest-impact open review items with
target context attached:

```powershell
python scripts\ncs_harness.py review-priority --limit 20 --per-issue-type-limit 5
```

The default priority set focuses on training-goal concept links, task-KSA
relations, core ontology concepts, criteria format issues, API mismatches, and
suspected typos. Bulk duplicate/short-KSA hygiene issues are intentionally
excluded from this operator queue.

Export a stable JSONL seedpack when a human reviewer needs an auditable input
file:

```powershell
python scripts\ncs_harness.py export-review-seedpack --limit 50 --per-issue-type-limit 5 --out reports\review_seedpack.jsonl --markdown-out reports\review_seedpack.md --source-report-path reports\review_priority.md
```

The seedpack is export-only. It leaves `decision`, `reviewer_id`, `reviewed_at`,
and `rationale` empty for a person to fill later, and it does not mutate the DB
or mark anything `human_reviewed`. Use it to keep manual approval separate from
model refinement and raw-source preservation.

Review seedpacks and triage reports are written as UTF-8. On Windows, inspect
them with an explicit encoding to avoid console mojibake:

```powershell
Get-Content -Encoding utf8 reports\review_seedpack.jsonl -TotalCount 3
Get-Content -Encoding utf8 reports\transition_scenario_seedpack.md
```

Transition gold scenarios have a separate review seedpack because they gate
recommendation readiness rather than ontology/link review:

```powershell
python scripts\ncs_harness.py export-transition-scenario-seedpack --review-status candidate,candidate_auto --scenario-limit 10 --recommendation-limit 5 --out reports\transition_scenario_seedpack.jsonl --markdown-out reports\transition_scenario_seedpack.md --source-report-path reports\training_transition_evaluation.md
```

This command is also export-only. It bundles each candidate scenario with the
current recommendation hits, recall, precision, and expected courses so a human
reviewer can approve, reject, or defer the scenario in a later audited apply
step.

Build a read-only triage view when you want one operator handoff across quality
gates, review-priority items, and transition scenario review candidates:

```powershell
python scripts\ncs_harness.py review-triage --quality-report reports\quality_gates_with_transition.json --review-priority-report reports\review_priority.json --transition-seedpack reports\transition_scenario_seedpack.jsonl --out reports\review_triage.json --markdown-out reports\review_triage.md
```

`review-triage` reads existing artifacts only. It categorizes warnings into
human-review debt, collection stability, and data-quality work; it does not
update review statuses, apply seedpack decisions, or mutate source data.

## Transition Evaluation

Use trusted-only evaluation for readiness reporting:

```powershell
python scripts\ncs_harness.py evaluate-training-transitions --trusted-only --limit 5
```

Use `--review-status` for candidate exploration and `--scenario-limit` for fast
sample checks. `--limit` controls recommendations per scenario, while
`--scenario-limit` controls how many scenarios are evaluated.

```powershell
python scripts\ncs_harness.py evaluate-training-transitions --review-status candidate,candidate_auto --limit 5 --scenario-limit 10
```

`generate-training-transition-eval-set` writes separate report sections for
`all_non_rejected`, `trusted_reviewed`, and `candidate_or_auto` so candidate
metrics are not mistaken for reviewed readiness. In JSON output, the mixed
summary is named `all_non_rejected_evaluation`; readiness automation should read
`evaluations.trusted_reviewed`.
