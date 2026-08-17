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

`inspect` defaults to the active NCS/AI-HR table set so it remains fast on the
large local SQLite DB. Use `python scripts\ncs_harness.py inspect --full` only
when legacy/reference table counts are needed.

## Workspace Hygiene

This repository can contain multi-GB Git LFS working files such as
`data/processed/ncs.db`. A plain `git status` can invoke the LFS clean/filter
path on those files and create huge `.git/lfs/tmp` files. Use the bounded status
command below during automation:

```powershell
git -c filter.lfs.clean=cat -c filter.lfs.smudge=cat -c filter.lfs.process= -c filter.lfs.required=false status --short --untracked-files=no -- . ":(exclude)data/processed/*.db" ":(exclude)data/processed/*.db-*" ":(exclude)data/raw/*.xlsx" ":(exclude)data/raw/*.xls" ":(exclude)data/ocr/tessdata/*.traineddata"
```

Use the workspace hygiene command before and after long runs. It is report-only
by default and executes the safe status command with LFS clean/smudge/process
filters disabled and the heavy LFS paths excluded. It stores only a bounded
`safe_git_status` summary, runs LFS prune dry-run, reports prunable LFS object
size, lists large files above the configured threshold, and reports `.git` LFS
temp size plus Python cache size. Add `--apply` only when removing regenerable
LFS cache/temp files, prunable LFS local objects, Python caches, and orphaned
SQLite sidecar files is intended.

SQLite `*.db-wal`, `*.db-shm`, and `*.db-journal` files for an existing DB are
reported only. Stop DB users and checkpoint the DB before any manual cleanup.
The automated cleanup removes only orphaned sidecars whose base DB file is
missing.

```powershell
python scripts\ncs_harness.py workspace-hygiene
python scripts\ncs_harness.py workspace-hygiene --apply
python scripts\ncs_harness.py workspace-hygiene --large-file-threshold-mb 256 --large-file-limit 40 --out reports\workspace_hygiene.json
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

Generate a read-only coverage plan before broad all-unit collection. The plan
does not call the API or write the database; it calculates the remaining guarded
batch count and writes JSON/Markdown/CSV artifacts for operator timing.

```powershell
python scripts\ncs_harness.py qualification-coverage-plan --target-ratio 0.9 --batch-size 100 --ncs006-checkpoint-path reports\checkpoint_ncs006_element_api_status_<DATE>_current.json --out reports\qualification_collection_coverage_plan_<DATE>.json --markdown-out reports\qualification_collection_coverage_plan_<DATE>.md --csv-out reports\qualification_collection_coverage_plan_<DATE>.csv
```

Before retrying qualification API errors, generate a read-only hygiene report.
This does not call the API or mutate retry metadata; it classifies cached
errors, shows missing `last_error_type` / `attempt_count` / `next_retry_at`
metadata, reports the gap to the release coverage target, and warns when broad
retries are likely to hit rate limits again.

```powershell
python scripts\ncs_harness.py qualification-retry-hygiene --limit 50 --ncs006-checkpoint-path reports\checkpoint_ncs006_element_api_status_<DATE>_current.json --out reports\qualification_retry_hygiene.json --markdown-out reports\qualification_retry_hygiene.md
```

Only use `retry-qualification-errors` after inspecting that report, preferably
with a small `--limit-units`, a request delay, and low retry count while
rate-limit errors dominate.

Use the rate-limit circuit breaker on every broad qualification retry or
all-unit collection batch:

```powershell
python scripts\ncs_harness.py retry-qualification-errors --limit-units 50 --num-of-rows 50 --max-pages 1 --request-delay 2 --max-retries 1 --retry-backoff-seconds 30 --stop-after-rate-limit-errors 3 --ncs006-checkpoint-path reports\checkpoint_ncs006_element_api_status_<DATE>_current.json --report-path reports\qualification_error_report.md
python scripts\ncs_harness.py collect-qualification-items --all-units --limit-units 100 --num-of-rows 50 --max-pages 1 --request-delay 2 --max-retries 1 --retry-backoff-seconds 30 --stop-after-rate-limit-errors 3 --ncs006-checkpoint-path reports\checkpoint_ncs006_element_api_status_<DATE>_current.json
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

Short representative KSA labels are a separate candidate layer. They do not
overwrite raw KSA text or `ontology_concepts.concept_name`.

```powershell
python scripts\ncs_harness.py preprocess-ncs-ontology --no-relations --ksa-labels --reset-ksa-labels
python scripts\ncs_harness.py report-ksa-label-candidates --out reports\ksa_label_candidate_preprocessing_20260621.json --markdown-out reports\ksa_label_candidate_preprocessing_20260621.md --review-jsonl-out reports\ksa_label_review_seedpack_20260621.jsonl --review-csv-out reports\ksa_label_review_seedpack_20260621.csv
python scripts\ncs_harness.py report-ksa-label-candidates --major-code 02 --out reports\ksa_label_candidate_preprocessing_major02_20260621.json --markdown-out reports\ksa_label_candidate_preprocessing_major02_20260621.md --review-jsonl-out reports\ksa_label_review_seedpack_major02_20260621.jsonl --review-csv-out reports\ksa_label_review_seedpack_major02_20260621.csv
```

The report follows the review chain `raw_ksa -> atomic_ksa ->
representative_concept -> short_label_candidate -> term_definition_candidate ->
criteria_task_evidence`. `short_label_candidate` is the user-facing
`단어형 대표 라벨 후보`; the physical field is
`ontology_concept_label_candidates.label_text`. It is review context only,
`status_update_allowed=false`, and trusted statuses in label candidates are
treated as anomalies rather than progress.
Major-scoped reports and dashboard rows scope label candidates by
`ontology_concept_label_candidates.source_scope_key` plus the label row's
`source_ksa_id` / `source_atomic_id`, not just by shared `concept_id`. This
prevents a shared concept from showing a label candidate sourced from a
different NCS major or sub-field as if it belonged to the current scope.
Rows without source provenance are counted as anomalies and are not displayed as
valid scoped dashboard label candidates.
The optional review seedpack lists collision, generic, low-confidence, and
label-quality candidates with blank human decision fields. It is a review
prompt, not an approval artifact. Each row carries `review_focus`,
`allowed_decisions`, `term_definition_candidate`, `term_definition_evidence`,
`task_evidence_count`, `task_evidence_preview`, `task_evidence_refs`,
`criteria_ids`, and `criteria_text_preview` so a reviewer can inspect the
source KSA, short-label reduction, candidate definition, and criteria/task
evidence without opening the SQLite tables directly.
The seedpack also leaves `raw_to_label_checked` blank and sets
`status_update_allowed=false` plus `trusted_status_write_allowed=false`. A
human reviewer should fill `raw_to_label_checked` only after checking the row's
`raw_ksa_text -> atomic_ksa_text -> concept_name -> label_text` chain.

For bulk KSA short-label triage after an audited HR sample exists, use the
read-only auto-triage report:

```powershell
python scripts\ncs_harness.py ksa-label-auto-triage-report --trusted-major-code 02 --trusted-middle-code 02 --trusted-small-code 02 --out reports\ksa_label_auto_triage.json --markdown-out reports\ksa_label_auto_triage.md --csv-out reports\ksa_label_auto_triage.csv
```

The report preserves the legacy `recommendation_bucket` values and adds an
operator-facing `classification_v2` view:

- `auto-pass-candidate`: spot-check candidate only; not human approval.
- `modify-recommended`: improve the label transformation before row review.
- `human-sample-needed`: sample by major, pattern, and family instead of bulk
  clicking rows.
- `domain-expert-needed`: route acronym, symbol-heavy, or domain-specific rows
  to a specialist sample.
- `already-trusted-review` and `missing-label-gap` are auxiliary states and are
  excluded from decision CSV rows.

This command writes reports only. It must keep `status_update_allowed=false`,
`db_writes=false`, and `approval_claim=false`, and it must never set
`human_reviewed`, `accepted`, or `reviewed`.

After a policy-v2 auto-triage report exists, build an operator sampling plan
from that JSON instead of asking a reviewer to click every decision row:

```powershell
python scripts\ncs_harness.py ksa-label-policy-v2-sampling-plan --source-report reports\ksa_label_auto_triage.json --out reports\ksa_label_policy_v2_sampling_plan.json --markdown-out reports\ksa_label_policy_v2_sampling_plan.md --csv-out reports\ksa_label_policy_v2_sampling_plan.csv
```

The sampling plan is report-only and reads the source JSON; it does not open the
SQLite DB. It validates that the source report has `status_update_allowed=false`,
`db_writes=false`, and `approval_claim=false`, then estimates a stratified sample
by major and `classification_v2`. The CSV keeps `decision`, `reviewer_id`,
`reviewed_at`, and `rationale` blank. Completing the sample is not full approval,
and the plan must not be used to write `human_reviewed`, `accepted`, or
`reviewed`.
The source report must be all-scope: `scope_policy.target_scope_is_filtered`
must be `false`. A major/middle/small/sub-scoped auto-triage report is a local
diagnostic view and is rejected as a sampling-plan source.

Dashboard operators should treat `/ksa-label-auto-triage` as the read-only
triage surface only. The release/bulk planning path is:

1. Generate an all-scope `ksa-label-auto-triage-report` with no
   major/middle/small/sub filters.
2. Generate `ksa-label-policy-v2-sampling-plan` from that all-scope JSON.
3. Use the policy-v2 operator handoff index or next-actions report to find the
   canonical CSV.
4. Run `ksa-label-policy-v2-scope-diff` only as a scoped diagnostic when a
   major-specific screen or report appears to disagree with all-scope counts.

Scoped dashboard counts are local views. They are useful for drilling into a
major such as HR or chemical/bio, but they are not canonical bulk workload
counts and scoped auto-pass rows are not global approval evidence.

When comparing a scoped major run with the all-scope policy-v2 report, use the
scope-diff diagnostic:

```powershell
python scripts\ncs_harness.py ksa-label-policy-v2-scope-diff --all-scope-report reports\ksa_label_auto_triage_all.json --scoped-report reports\ksa_label_auto_triage_major02.json --major-code 02 --out reports\ksa_label_policy_v2_scope_diff_major02.json --markdown-out reports\ksa_label_policy_v2_scope_diff_major02.md
```

This command is count-only and diagnostic-only. It highlights cases where a
major-scoped run changes `classification_v2` counts because the collision or
context window is smaller than the all-scope run. Use all-scope counts for
release and cross-major workload planning. Scoped auto-pass counts are local
diagnostics only and must not be treated as trusted approval.

To open the KSA definition dashboard on Windows, run `Run_KSA_Definition_Dashboard.cmd`
or:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_dashboard.ps1 -OpenPath /ksa-definitions -Restart
```

Use the `Label State` filter to inspect the new candidate layer directly:

- `Shortened candidate`: long KSA phrase was reduced into a shorter candidate label.
- `Unchanged/already short`: source KSA was already short enough.
- `Missing label`: no label candidate exists for the row's concept in the current scope.
- `Collision review`: one normalized label maps to multiple concepts and needs human triage.
- `Generic review`: label is too broad or too short for direct use.

`Definition State=definition_status=defined` shows only trusted definition-status
rows; model-preprocessed candidate definitions remain under candidate states.

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

## SQF Context Score

Use the SQF context report only as an explainability and review surface for job
world proximity. It must not change training recommendation ranking, course
scores, or any human-review status.

```powershell
python scripts\ncs_harness.py report-sqf-context-score --current-query "노무관리" --target-query "인사기획" --out reports\sqf_context_score_nomu_to_insa_20260621.json --markdown-out reports\sqf_context_score_nomu_to_insa_20260621.md
```

The report contract includes `context_only=true`,
`recommendation_score_mutated=false`, `sqf_used_as_training_score=false`,
`approval_ready=false`, and `status_update_allowed=false`. Treat all SQF->NCS
links as review context unless a human process upgrades their status.

The model must not treat an NCS sub-classification as an SQF job. NCS
classification is used only to resolve query scope and find NCS competency
units. SQF context is computed from explicit SQF level-based-job records linked
N:M to NCS competency units through `sqf_ncs_matches`.

SQF level numbers are sector-contextual. The harness compares SQF level
distance only when both sides are in the same SQF sector. Cross-sector pairs are
reported with `level_comparison_status=not_comparable_cross_sector` and no
effective `sqf_level_distance`, even if the raw level numbers look close. When
one side has no usable level number in the same sector, the pair is reported as
`level_missing_same_sqf_sector` and the level component contributes no score.

Do not infer `REQUIRED` or `OPTIONAL` from an NCS unit or from lexical matching.
That status belongs to the SQF level-based-job to NCS-unit mapping relation. The
current `sqf_ncs_matches` table does not expose a trusted requirement type, so
the SQF context report leaves required/optional classification unavailable.
Course alignment checks must also remain separate from official SQF recognition;
`OFFICIALLY_RECOGNIZED` requires an external confirmed source, not automation.

Invalid task locator input for `recommend-training-for-task` and invalid
`review-triage` inputs print structured JSON errors and exit non-zero. Treat
those as failed automation steps.

## Ontology-Adjusted Transferability Batch

Use the ontology-transferability batch commands when building same-NCS-subclass
education-system drafts across all NCS major fields. The batch signal is for
planning and review prioritization only; it must not mark rows
`human_reviewed`, `accepted`, or `reviewed`.

Generate per-major artifacts and a reproducible run manifest:

```powershell
python scripts\ncs_harness.py build-ontology-transferability-major-run --date-stamp 20260618 --out-dir reports --out reports\ontology_transferability_major_run_20260618.json --markdown-out reports\ontology_transferability_major_run_20260618.md
```

Each major produces JSON, Markdown, and top-pair CSV artifacts such as
`reports\ontology_transferability_major02_20260618.json` and
`reports\ontology_transferability_major02_pairs_20260618.csv`. The command opens
the SQLite DB read-only and records failed majors in the manifest instead of
silently producing a green downstream report.

Build review and calibration artifacts from the manifest:

```powershell
python scripts\ncs_harness.py summarize-ontology-transferability-run --run reports\ontology_transferability_major_run_20260618.json --out reports\ontology_transferability_field_review_regen_20260618.json --markdown-out reports\ontology_transferability_field_review_regen_20260618.md
python scripts\ncs_harness.py export-ontology-transferability-review-seedpack --run reports\ontology_transferability_major_run_20260618.json --out reports\ontology_transferability_review_seedpack_regen_20260618.jsonl --markdown-out reports\ontology_transferability_review_seedpack_regen_20260618.md
python scripts\ncs_harness.py calibrate-ontology-transferability-thresholds --run reports\ontology_transferability_major_run_20260618.json --out reports\ontology_transferability_calibration_20260618.json --markdown-out reports\ontology_transferability_calibration_20260618.md
python scripts\ncs_harness.py audit-ontology-transferability-education-systems --run reports\ontology_transferability_major_run_20260618.json --out reports\ontology_transferability_education_system_audit_20260618.json --markdown-out reports\ontology_transferability_education_system_audit_20260618.md
```

The seedpack is export-only. Its batch and item records keep decision,
reviewer, reviewed-at, and rationale fields blank for human reviewers.
The education-system audit is also report-only. It aggregates C1-1/C1-2/C2-1/C2-2
guide-readiness signals across major artifacts, including matrix rows,
course-link coverage, required/optional grouping, delivery-operation evidence,
facility-fit status, and human-review gates. It must not be treated as human
approval. A structurally valid draft with pending human-review rows reports
`contract_ok=true`, `approval_ready=false`, `status=review_required`, and
top-level `ok=false`; automation must not collapse these states into a green
approval signal.

Diagnose no-course-link scopes before deciding whether to collect more training
course data or repair linking:

```powershell
python scripts\ncs_harness.py diagnose-ontology-transferability-course-links --run reports\ontology_transferability_major_run_20260618.json --out reports\ontology_transferability_course_link_gap_diagnostic_20260618.json --markdown-out reports\ontology_transferability_course_link_gap_diagnostic_20260618.md
```

This command is report-only. It reads the major-run artifacts plus the training
course table and separates likely source absence, name-normalization gaps, and
possible unit-link gaps without inserting links or changing review statuses.
Name-similar courses outside the target NCS major are not promoted as link-gap
candidates; they are reported as `cross_scope_name_only` so reviewers can treat
them as adjacent references or query-normalization clues, not as direct
education-system evidence. A successful diagnostic can still report
`contract_ok=true`, `approval_ready=false`, and `status=review_required`; `ok`
is retained only as an artifact-generation compatibility bit.

Prepare human-review candidates for the scopes where the diagnostic found
exact or similar training-course rows:

```powershell
python scripts\ncs_harness.py review-ontology-transferability-course-link-candidates --gap-diagnostic reports\ontology_transferability_course_link_gap_diagnostic_20260618.json --out reports\ontology_transferability_course_link_candidate_review_20260618.json --markdown-out reports\ontology_transferability_course_link_candidate_review_20260618.md
```

This command is also report-only. It exposes candidate course rows, hours,
methods, facilities, and training-goal previews so a reviewer can decide whether
the gap is a unit-link repair, name-normalization issue, or source-data absence.
Only same-major-or-tighter scope fits are emitted as link candidates. Cross-major
name matches stay out of the candidate table by default. It must not be used to
auto-write course links or review statuses, and its `approval_ready` field must
remain false until a human reviewer decides.

Create the external spot-check plan and method work queue:

```powershell
python scripts\ncs_harness.py plan-ontology-transferability-spotchecks --seedpack reports\ontology_transferability_review_seedpack_regen_20260618.jsonl --out reports\ontology_transferability_spotcheck_plan_20260618.json --markdown-out reports\ontology_transferability_spotcheck_plan_20260618.md
python scripts\ncs_harness.py plan-ontology-transferability-method-work-queue --calibration reports\ontology_transferability_calibration_20260618.json --seedpack reports\ontology_transferability_review_seedpack_regen_20260618.jsonl --field-review reports\ontology_transferability_field_review_regen_20260618.json --spotcheck-plan reports\ontology_transferability_spotcheck_plan_20260618.json --external-spotcheck reports\ontology_transferability_p0_external_spot_check_20260618.md --course-link-gap-diagnostic reports\ontology_transferability_course_link_gap_diagnostic_20260618.json --out reports\ontology_transferability_method_work_queue_20260618.json --markdown-out reports\ontology_transferability_method_work_queue_20260618.md
```

Run the artifact audit before using the batch outputs in reports or dashboards:

```powershell
python scripts\ncs_harness.py audit-ontology-transferability-artifacts --run reports\ontology_transferability_major_run_20260618.json --seedpack reports\ontology_transferability_review_seedpack_regen_20260618.jsonl --spotcheck-plan reports\ontology_transferability_spotcheck_plan_20260618.json --method-work-queue reports\ontology_transferability_method_work_queue_20260618.json --out reports\ontology_transferability_artifact_audit_20260618.json --markdown-out reports\ontology_transferability_artifact_audit_20260618.md
```

The audit checks the generated major artifacts, `recommended_path` stages,
`training_system_matrix` row fields, `needs_review` status, seedpack blank
decision fields, spot-check count consistency, and method-work-queue membership.
It exits non-zero on any contract issue. This is a structural audit, not a
release-readiness decision.

Run the separate release gate when the artifacts are being used as a release
proof:

```powershell
python scripts\ncs_harness.py gate-ontology-transferability-release --calibration reports\ontology_transferability_calibration_20260618.json --method-work-queue reports\ontology_transferability_method_work_queue_20260618.json --artifact-audit reports\ontology_transferability_artifact_audit_20260618.json --out reports\ontology_transferability_release_gate_20260618.json --markdown-out reports\ontology_transferability_release_gate_20260618.md
```

The release gate intentionally fails while manual-priority scopes,
no-course-link scopes, P0 queue items, or open method-work-queue items remain.
That blocked result is useful evidence: it says the batch is reproducible and
auditable, but not yet ready for unattended release claims.

Interpretation rules:

- Do not tune global thresholds from `avg_adjusted` alone. Review
  `avg_exact`, `avg_adjusted_minus_exact`, `baseline_heavy_pair_ratio`, course
  links, hours, methods, facilities, and external evidence together.
- Do not emit `required` rows when exact KSA overlap is weak, baseline
  dependency is high, or direct training-course links are missing. Directly
  course-linked rows may remain `recommended` or `optional` with human-review
  caveat flags, but weak evidence must not be promoted to `required`.
- Keep no-course-link rows review-gated until course evidence or a human
  reviewer supports stronger use.
- Use role overlays for manager/team-lead targets in the route/planner layer,
  not in the global batch transferability score.
- External spot-check evidence can guide method changes, but it is not scored
  source training data.

## Report-Grounded Transferability Human Review

When using the 2026 HRD report as the Human Review basis, audit transferability
against the report's job-movement model instead of treating DB review statuses as
the decision source:

```powershell
python scripts\ncs_harness.py audit-report-grounded-transferability-review --report-text tmp\html_extract\report_2_html_pages.txt --plan-json reports\aihr_plan_after_report_human_review_20260619.json --education-audit reports\ontology_transferability_education_system_audit_20260619.json --career-review reports\ncs_report_human_review_career_path_20260619.json --out reports\aihr_report_grounded_transferability_review_20260619.json --markdown-out reports\aihr_report_grounded_transferability_review_20260619.md
```

The audit checks that transferability evidence reflects the report's movement
logic: task/performance/KSA chains, level and authority fit, internal movement
and transition-support context, career-path horizontal movement across job-skill
types, vertical movement across skill levels, self-diagnosis gap evidence, and
training-standard delivery evidence. The expected result can still be
`status=review_required` and `approval_ready=false`; this is a Human Review
readiness report, not a DB write instruction or automatic approval signal.

## HRD Guide Reference

The project keeps the converted 2026 NCS HR practical guide inside
`docs/reference/` so development and automation can consult the same rubric
without reading from a user download folder.

Initial import:

```powershell
python scripts\ncs_harness.py preprocess-hrd-guide-reference --source <path-to-ncs_hrd_guide_codex_readable.md>
```

Rebuild from the project copy:

```powershell
python scripts\ncs_harness.py preprocess-hrd-guide-reference
```

Check guide prompt coverage against the live router:

```powershell
python scripts\ncs_harness.py hrd-guide-prompt-coverage --out reports\hrd_guide_prompt_coverage_20260618.json --markdown-out reports\hrd_guide_prompt_coverage_20260618.md
```

Generated artifacts:

- `docs\reference\ncs_hrd_guide_codex_readable.md`
- `docs\reference\ncs_hrd_guide_reference.index.json`
- `docs\reference\ncs_hrd_guide_reference.md`
- `docs\reference\ncs_hrd_guide_reference.chunks.jsonl`

Use the index as `framework_reference` only. It may define planning stages,
guide-trace checks, prompt coverage, and development rules. It must not create
source training rows, raise recommendation scores by itself, or overwrite human
review states. See `docs\NCS_HRD_GUIDE_REFERENCE.md`.

## AI-HR Education-System Demo

Use the AI-HR demo runner when the goal is to show the 2026 HR NCS
training-system guide as a working planning rubric. The guide is not ingested as
source training data; it is reflected in the response contract as
`training_system_summary`, `training_system_guide_trace`,
`training_system_matrix`, need classification, evidence directness, and
delivery fit fields.

Guide-aligned planning stages:

- `C1-1`: investigate courses and map job, task, performance criteria, and KSA.
- `C1-2`: review training necessity and produce a confirmed course list for
  human decision.
- `C2-1`: assign education type and level, then build the education-system
  matrix.
- `C2-2`: prepare annual operation and management-plan fields from the matrix.

Automation may prepare evidence for `human_review`, but it must not mark
`human_reviewed`, `accepted`, or `reviewed` states without an explicit human
decision.

## Query Routing Contract

The MCP follows the Law MCP pattern of routing a natural-language request before
choosing a concrete tool. `src/ncs_mcp/query_router.py` maps an intent to a
scenario, recommended tool, inferred params, missing params, suggested pipeline,
and risk flags. `ncs_discover_tools` includes this object as `query_route`.
Automation agents can inspect the same contract without starting the MCP server:

```powershell
python scripts\ncs_harness.py route-ncs-query "from labor management to HR planning education system"
python scripts\ncs_harness.py route-ncs-query "review quality issue for training goal link" --include-operator-tools
```

Primary scenarios:

- `education_system_design`: route to `plan_ncs_education_path`.
- `training_transition`: route to `recommend_training_transition`.
- `task_training`: route to `recommend_training_for_task`.
- `task_transition`: route to `recommend_task_transitions`.
- `evidence_analysis`: route to `ncs_analysis`.
- `operator_review`: route to hidden operator/review tools when enabled.
- `structure_search`: route to `ncs_search`.

Automation agents should inspect `query_route.missing_params` before execution.
Operator-review routes use `get_quality_issues` as the read-only discovery
surface and may infer filters such as
`target_type=training_goal_concept_link` from "training goal link" or
`훈련목표 KSA 링크` prompts. Guarded mutation tools such as
`review_training_goal_concept_link` still require an explicit human decision and
must not be called by route automation.
If `risk_flags` contains `official_or_legal_claim_risk`, public copy and demo
artifacts must state that the output is a prototype training recommendation, not
official approval, qualification recognition, or legal eligibility.

The standard one-shot command regenerates the baseline transition JSON, an
alias-heavy JSON, and the HTML dashboard together:

```powershell
python scripts\ncs_harness.py run-aihr-plan-demo --out-dir reports --base-name aihr_plan_demo_20260617
```

The generated artifacts should include:

- `reports\aihr_plan_demo_20260617.json` public redacted JSON
- `reports\aihr_plan_demo_alias_20260617.json` public redacted JSON
- `reports\aihr_plan_demo_internal_20260617.json` internal audit JSON
- `reports\aihr_plan_demo_alias_internal_20260617.json` internal audit JSON
- `reports\aihr_plan_demo_20260617.html`

After starting the dashboard, open the AI-HR route:

```powershell
python scripts\ncs_dashboard.py --host 127.0.0.1 --port 8765
# http://127.0.0.1:8765/aihr-live
# http://127.0.0.1:8765/aihr-plan-demo
```

For a dated release/queue bundle, start the dashboard with the same readiness,
queue-status, and queue-run artifacts used by verification:

```powershell
python scripts\ncs_dashboard.py --host 127.0.0.1 --port 8765 `
  --aihr-readiness-json reports\overnight_sessions\readonly_refresh\aihr_release_readiness_20260629_next.json `
  --aihr-agent-queue-status-json reports\overnight_sessions\readonly_refresh\aihr_agent_queue_status_20260629_next.json `
  --aihr-agent-queue-run-json reports\overnight_sessions\readonly_refresh\aihr_agent_queue_run_20260629_next.json

powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_dashboard.ps1 -Restart `
  -AihrReadinessJson reports\overnight_sessions\readonly_refresh\aihr_release_readiness_20260629_next.json `
  -AihrAgentQueueStatusJson reports\overnight_sessions\readonly_refresh\aihr_agent_queue_status_20260629_next.json `
  -AihrAgentQueueRunJson reports\overnight_sessions\readonly_refresh\aihr_agent_queue_run_20260629_next.json
```

`/aihr-live` is the live, no-save runner. It accepts a current query and target
query in the browser, calls the same NCS education-plan engine used by
`plan-ncs-education-path`, and returns public-redacted JSON through
`POST /api/aihr-plan`.

The live response must also expose `query_route` with schema
`ncs_query_route_v1`, `tool=plan_ncs_education_path`,
`expected_tool_chain`, `route_contract`, and a stable `route_fingerprint`.
The browser renders this as Route Evidence so an operator can inspect the
routed tool chain, guard flags, and fingerprint beside the generated education
system matrix.

The dashboard auto-discovers standard filenames only for demo HTML, readiness
JSON, review-triage JSON, current `reports\aihr_agent_queue*.json` queue
artifacts, legacy/alias `reports\aihr_agent_work_queue*.json` queue artifacts,
queue-status JSON, and queue-run JSON.
For custom demo names, set `NCS_AIHR_DEMO_JSON_PATH`,
`NCS_AIHR_DEMO_HTML_PATH`, and `NCS_AIHR_READINESS_JSON_PATH` before starting
the dashboard. For a custom review-triage board, set
`NCS_AIHR_REVIEW_TRIAGE_JSON_PATH`. For the current release queue artifact
(`reports\aihr_agent_queue_20260617.json`), set
`NCS_AIHR_AGENT_QUEUE_JSON_PATH`. For a custom queue preflight status, set
`NCS_AIHR_AGENT_QUEUE_STATUS_JSON_PATH`. For a custom queue run artifact, set
`NCS_AIHR_AGENT_QUEUE_RUN_JSON_PATH`.
When running `verify-aihr-dashboard`, also pass
`--static-artifact-dir <same-artifact-directory>` so static artifact snapshots
and live queue endpoints are checked against the same queue lineage.
For dated release artifacts, the dashboard verification used by
`release_readiness_report.py` must point back to the same dated
release-readiness JSON and `agent_work_queue_path` that the release command is
emitting. A same-day but stale dashboard proof is a lineage blocker even when
all file names share the same `YYYYMMDD` stamp.

Demo contract checks:

- `ok=true` and `view=ncs_education_plan`.
- `recommended_path` includes the scope confirmation, core gap training, and
  supporting or adjacent training stages.
- `training_system_matrix` has at least one row.
- `training_system_guide_trace.schema` is
  `aihr_training_system_guide_trace_v1`, and its checks include `job_scope`,
  `task_ksa`, `course_link`, `required_optional`, `level_delivery`, and
  `human_review`.
- Every matrix row exposes planner grouping fields: `job_scope`,
  `target_level_band`, `education_type`, `required_optional_basis`,
  `delivery_operation`, `planner_grouping`, `task_ksa_basis`,
  `facility_constraint_fit`, and `human_review`.
- `task_ksa_basis` includes basis types plus target-scope, gap, training-goal,
  and covered-element evidence; `facility_constraint_fit` includes status,
  requested, available, matched, missing, and rationale fields.
- Every matrix row exposes `course_fit.level`, `course_fit.hours`,
  `course_fit.methods`, and `course_fit.facilities`.
- `audit.sqf_used=false` and `audit.learning_modules_used=false`.
- JSON and HTML do not expose `source_payload`, service keys, or raw auth
  markers.
- Public JSON does not expose internal operational metadata such as
  `relation_id`, `created_at`, `updated_at`, `review_status`, or
  `data_sources`. Keep `_internal` JSON artifacts for audit only.

Build the release/readiness view after regenerating the demo:

```powershell
python scripts\export_mcp_tool_contract.py --out reports\mcp_tool_contract_20260617.json
python scripts\ncs_harness.py audit-aihr-guide-surface --demo-json reports\aihr_plan_demo_20260617.json --demo-json reports\aihr_plan_demo_alias_20260617.json --out reports\aihr_guide_surface_audit_20260617.json --markdown-out reports\aihr_guide_surface_audit_20260617.md
python scripts\release_readiness_report.py --quality-report reports\aihr_quality_gates_with_transition_20260617.json --contract reports\mcp_tool_contract_20260617.json --demo-json reports\aihr_plan_demo_20260617.json --demo-json reports\aihr_plan_demo_alias_20260617.json --demo-html reports\aihr_plan_demo_20260617.html --dashboard-verification reports\aihr_dashboard_surface_verification_20260617.json --out reports\aihr_release_readiness_20260617.json --markdown-out reports\aihr_release_readiness_20260617.md --agent-queue-out reports\aihr_agent_queue_20260617.json --agent-queue-markdown-out reports\aihr_agent_queue_20260617.md
python scripts\ncs_harness.py agent-queue-status --queue reports\aihr_agent_queue_20260617.json --out reports\aihr_agent_queue_status_20260617.json --markdown-out reports\aihr_agent_queue_status_20260617.md
# http://127.0.0.1:8765/aihr-readiness
# http://127.0.0.1:8765/aihr-review-board
# http://127.0.0.1:8765/aihr-agent-queue
# http://127.0.0.1:8765/aihr-agent-queue-status
# http://127.0.0.1:8765/api/aihr-agent-queue-status
# http://127.0.0.1:8765/aihr-agent-queue-run
# http://127.0.0.1:8765/api/aihr-agent-queue-run
# http://127.0.0.1:8765/aihr-live
```

`agent-queue-status` is a preflight check only. It does not execute queue
commands. Use `ready_to_start` items for automated report regeneration, keep
`manual_ready` items operator-controlled, and fix any `blocked_*` item before
running subagents.

The current release queue artifact is
`reports\aihr_agent_queue_20260617.json` / `.md`. Older
`reports\aihr_agent_work_queue_20260617.*` files may exist as legacy aliases.
When automating from a readiness run, prefer the latest readiness JSON's
`agent_work_queue_path` value over hard-coded queue names.

The training-goal review queue item must run `review-triage` with the latest
transition scenario seedpack. Regenerate
`reports/aihr_transition_scenario_seedpack_20260617.jsonl` before triage when
the transition-evaluation evidence has changed.

To verify the running dashboard surface end to end, start the dashboard and run:

```powershell
python scripts\ncs_harness.py verify-aihr-dashboard --base-url http://127.0.0.1:8765 --out reports\aihr_dashboard_surface_verification_20260617.json --markdown-out reports\aihr_dashboard_surface_verification_20260617.md
```

This command checks static artifact presence plus `/aihr-live`,
`/aihr-plan-demo`, `/aihr-readiness`, `/aihr-review-board`,
`/aihr-agent-queue`, `/aihr-query-router`, `/aihr-agent-queue-status`,
`/api/aihr-agent-queue-status`, `/aihr-agent-queue-run`,
`/api/aihr-agent-queue-run`, and `POST /api/aihr-plan`. It exits non-zero if
the live runner fails, the queue status or queue run schema is wrong, public
demo/readiness pages are missing, required static artifacts are missing or empty, or the
live JSON response leaks public-redaction markers such as `source_payload` or
`relation_id`, or the education-system matrix is missing planner grouping
fields. By default it verifies two live-plan scenarios: `노무관리 -> 인사기획` and
`복무관리 -> 인사기획`. Use `--single-plan-check` only when a faster
one-scenario check is enough.

The same verification fails if `query_route` is missing schema
`ncs_query_route_v1`, `tool=plan_ncs_education_path`, the expected education
plan tool chain, or the route fingerprint.

The live-plan proof fields must include empty `missing_matrix_fields`,
`missing_plan_fields`, `missing_guide_trace_fields`, and
`missing_query_route_fields` arrays for every checked scenario. These prove the
live response contains `recommended_path`, `training_system_matrix`,
`task_ksa_basis`, `facility_constraint_fit`, `human_review`, `query_route`, and
`training_system_guide_trace`.

The verification JSON includes `static_artifacts` snapshots for the public demo
JSON/HTML, release-readiness JSON, agent queue-status JSON, agent queue-run
JSON, HRD guide prompt-coverage JSON, AI-HR guide surface audit JSON, and
ontology-transferability education-system audit JSON. It also snapshots
read-only API linkage and qualification guard reports, including
`qualification_retry_hygiene_*.json` and
`qualification_collection_coverage_plan_*.json`, so guarded collection planning
is visible without running the API.
Release readiness treats this snapshot as part of the dashboard surface
contract.
The queue-run snapshot is valid release evidence only when it is an actual
non-dry-run execution, has bounded/suppressed output tails, and has
`failed_count=0`, `acceptance_failed_count=0`, `skipped_unsafe_count=0`, and no
`run_statuses` beginning with `failed`. A failed auto-runnable queue item must
fail the dashboard/release contract instead of being counted as green
execution evidence.
Safe read-only checkpoints can still be `review_gated` when they expose an open
human-review gate, such as legacy reviewed rows without packet-backed
provenance. In that case `verify-aihr-dashboard` reports
`review_gated_checkpoint_artifacts` under the static artifact check instead of
failing the dashboard surface. Release readiness must still keep
`release_ready=false` through the explicit human-review blocker until a human
decision packet clears the gate. Automation must not clear this by setting
`human_reviewed`, `accepted`, `reviewed`, or `approval_ready=true`.
Internal review seedpack and triage artifacts are intentionally excluded from
the public/static dashboard inventory because they preserve operator review
state snapshots.
Release readiness also requires demo JSON/HTML and dashboard verification inputs;
omitting those proof artifacts is treated as a blocker, not as an unchecked pass.

Use lower-level commands only for custom scenarios:

```powershell
python scripts\ncs_harness.py plan-ncs-education-path --current-query "labor management" --target-query "HR planning" --limit 3 --no-save > reports\custom_aihr_plan.json
python scripts\ncs_harness.py render-aihr-plan-demo --out reports\custom_aihr_plan.html reports\custom_aihr_plan.json
```

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
file. Add Markdown for scanning and CSV when the reviewer will fill a decision
sheet in a spreadsheet:

```powershell
python scripts\ncs_harness.py export-review-seedpack --limit 50 --per-issue-type-limit 5 --out reports\review_seedpack.jsonl --markdown-out reports\review_seedpack.md --csv-out reports\review_seedpack.csv --source-report-path reports\review_priority.md
```

The seedpack is export-only. It leaves `decision`, `reviewer_id`, `reviewed_at`,
and `rationale` empty for a person to fill later, and it does not mutate the DB
or mark anything `human_reviewed`. Use it to keep manual approval separate from
model refinement and raw-source preservation. The CSV is encoded as
`utf-8-sig`, keeps the same blank decision columns, and is still not an apply
step.

Review seedpacks and triage reports are written as UTF-8. On Windows, inspect
them with an explicit encoding to avoid console mojibake:

```powershell
Get-Content -Encoding utf8 reports\review_seedpack.jsonl -TotalCount 3
Get-Content -Encoding utf8 reports\transition_scenario_seedpack.md
```

If Korean text looks corrupted in a terminal or editor, first re-open the same
artifact as UTF-8 and run a focused readability audit before assuming DB or
seedpack corruption. When the UTF-8 artifact and focused audit are clean, treat
the issue as viewer/display decoding noise, not as a reason to rewrite source
rows or apply review statuses.

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

### AI-HR Plan Review Workflow

Use this chain when turning AI-HR education-system demo artifacts into
one-at-a-time human review requests. The chain is report/CSV only until a
separate guarded import is designed and explicitly approved; it must not mark
`human_reviewed`, `accepted`, or `reviewed`.

```powershell
python scripts\ncs_harness.py export-aihr-plan-review-seedpack --demo-json reports\aihr_plan_review_basis_20260619.json --demo-json reports\aihr_plan_review_basis_alias_20260619.json --out reports\aihr_plan_review_seedpack_20260619.json --jsonl-out reports\aihr_plan_review_seedpack_20260619.jsonl --markdown-out reports\aihr_plan_review_seedpack_20260619.md
python scripts\ncs_harness.py export-aihr-plan-review-request-order --seedpack reports\aihr_plan_review_seedpack_20260619.json --out reports\aihr_plan_review_request_order_20260619.json --markdown-out reports\aihr_plan_review_request_order_20260619.md
python scripts\ncs_harness.py export-aihr-plan-review-decision-sheet --request-order reports\aihr_plan_review_request_order_20260619.json --out reports\aihr_plan_review_decision_sheet_20260619.json --csv-out reports\aihr_plan_review_decision_sheet_20260619.csv --html-out reports\aihr_plan_review_decision_sheet_20260619.html
python scripts\ncs_harness.py audit-aihr-plan-review-decisions --decision-sheet reports\aihr_plan_review_decision_sheet_20260619.csv --request-order reports\aihr_plan_review_request_order_20260619.json --out reports\aihr_plan_review_decision_audit_20260619.json --markdown-out reports\aihr_plan_review_decision_audit_20260619.md
python scripts\ncs_harness.py export-aihr-plan-review-next-request --seedpack reports\aihr_plan_review_seedpack_20260619.json --decision-audit reports\aihr_plan_review_decision_audit_20260619.json --request-order reports\aihr_plan_review_request_order_20260619.json --out reports\aihr_plan_review_next_request_20260619.json --markdown-out reports\aihr_plan_review_next_request_20260619.md --html-out reports\aihr_plan_review_next_request_20260619.html
python scripts\ncs_harness.py export-aihr-plan-review-group-packet --seedpack reports\aihr_plan_review_seedpack_20260619.json --request-order reports\aihr_plan_review_request_order_20260619.json --decision-audit reports\aihr_plan_review_decision_audit_20260619.json --major-code 02 --item-type training_system_row_review --out reports\aihr_plan_review_group_02_training_system_row_20260619.json --markdown-out reports\aihr_plan_review_group_02_training_system_row_20260619.md --html-out reports\aihr_plan_review_group_02_training_system_row_20260619.html
python scripts\ncs_harness.py export-aihr-plan-review-group-packet --seedpack reports\aihr_plan_review_seedpack_20260619.json --request-order reports\aihr_plan_review_request_order_20260619.json --decision-audit reports\aihr_plan_review_decision_audit_20260619.json --major-code 02 --item-type ontology_concept_review --out reports\aihr_plan_review_group_02_ontology_concept_20260619.json --markdown-out reports\aihr_plan_review_group_02_ontology_concept_20260619.md --html-out reports\aihr_plan_review_group_02_ontology_concept_20260619.html
python scripts\ncs_harness.py export-aihr-plan-review-group-packet --seedpack reports\aihr_plan_review_seedpack_20260619.json --request-order reports\aihr_plan_review_request_order_20260619.json --decision-audit reports\aihr_plan_review_decision_audit_20260619.json --major-code 02 --item-type training_goal_concept_link_review --out reports\aihr_plan_review_group_02_training_goal_concept_link_20260619.json --markdown-out reports\aihr_plan_review_group_02_training_goal_concept_link_20260619.md --html-out reports\aihr_plan_review_group_02_training_goal_concept_link_20260619.html
python scripts\ncs_harness.py export-aihr-plan-review-group-packet --seedpack reports\aihr_plan_review_seedpack_20260619.json --request-order reports\aihr_plan_review_request_order_20260619.json --decision-audit reports\aihr_plan_review_decision_audit_20260619.json --major-code 02 --item-type task_ksa_concept_relation_review --out reports\aihr_plan_review_group_02_task_ksa_concept_relation_20260619.json --markdown-out reports\aihr_plan_review_group_02_task_ksa_concept_relation_20260619.md --html-out reports\aihr_plan_review_group_02_task_ksa_concept_relation_20260619.html
python scripts\ncs_harness.py export-aihr-plan-review-packet-index --next-request reports\aihr_plan_review_next_request_20260619.json --decision-sheet reports\aihr_plan_review_decision_sheet_20260619.json --group-packet reports\aihr_plan_review_group_02_training_system_row_20260619.json --group-packet reports\aihr_plan_review_group_02_ontology_concept_20260619.json --group-packet reports\aihr_plan_review_group_02_training_goal_concept_link_20260619.json --group-packet reports\aihr_plan_review_group_02_task_ksa_concept_relation_20260619.json --require-full-group-coverage --out reports\aihr_plan_review_packet_index_20260619.json --markdown-out reports\aihr_plan_review_packet_index_20260619.md --html-out reports\aihr_plan_review_packet_index_20260619.html
python scripts\ncs_harness.py export-aihr-plan-review-workflow-handoff --seedpack reports\aihr_plan_review_seedpack_20260619.json --request-order reports\aihr_plan_review_request_order_20260619.json --decision-sheet reports\aihr_plan_review_decision_sheet_20260619.json --decision-audit reports\aihr_plan_review_decision_audit_20260619.json --next-request reports\aihr_plan_review_next_request_20260619.json --out reports\aihr_plan_review_workflow_handoff_20260619.json --markdown-out reports\aihr_plan_review_workflow_handoff_20260619.md
```

`export-aihr-plan-review-seedpack` preserves course evidence, task/KSA
contexts, `career_path_review_basis`, and `transition_review_basis` so the
reviewer sees why a course, concept, training-goal link, or task-KSA relation is
being asked about. It also carries NCS scope fields such as `ncs_cl_cd`,
`major_code`, `middle_code`, `small_code`, and `sub_code` into concept and
task-KSA review items so the handoff can group requests by NCS major and review
type. The 2026 guide remains a workflow rubric, not scored source training data.

`export-aihr-plan-review-next-request` writes the next one-at-a-time request as
JSON, Markdown, and optionally HTML. The request includes
`guide_review_checklist` with the six 2026 guide checks: `job_scope`,
`task_ksa`, `course_link`, `required_optional`, `level_delivery`, and
`human_review`.

`export-aihr-plan-review-group-packet` writes a human-readable packet for one
`major_code` and `item_type` group. Use it when the operator wants to review the
current major/type group side by side before answering the next one-at-a-time
request. For the current major `02`, the queue is split into
`training_system_row_review`, `ontology_concept_review`,
`training_goal_concept_link_review`, and `task_ksa_concept_relation_review`
packets. Each row includes `flag_sources` so merged warnings such as
`delivery:time_over` can be traced back to the exact source artifact and rank
that produced them. Rows also expose `career_path_refs` and
`transition_review_sources` so career-path movement evidence is visible for
review without becoming an approval signal. It remains read-only and carries no
approval or DB-write authority.

`export-aihr-plan-review-packet-index` writes the operator-facing packet index
as JSON, Markdown, and HTML. It validates that linked packet files exist and are
non-empty, checks row-count coverage when `--require-full-group-coverage` is
used, and blocks the index if any linked target exposes `source_payload`.
Coverage is checked against the linked decision-sheet CSV row count, not only
the summary JSON `row_count`; if the JSON count and CSV row count differ, the
packet index is blocked as stale or truncated evidence. Standard same-family
artifact discovery keeps multi-token date suffixes such as
`_20260627_extra_safe` intact.
When decision-sheet JSON records `csv_path` or `html_path` as a bare filename,
packet-index generation resolves that filename relative to the decision-sheet
JSON directory, not the process working directory. This keeps sibling sidecars
stable when the command is run from a different cwd.
Reviewer-facing packet-index JSON must not pass through broad collection
commands. Official learning-module gap collection context is represented as
a broad-command-withheld flag and
`legacy_diagnostic_collection=out_of_band_operator_workflow_only`. It also
sets `do_not_call_api_from_reviewer_artifact=true` and
`separate_operator_workflow_required=true`, so reviewer packets remain
decision/evidence surfaces rather than collection runbooks.

`export-aihr-plan-review-workflow-handoff` includes `grouping_summary` with
major/type counts, pending major/type counts, unknown-major samples, and the
current `next_request_group`. This is an operator guide for sequencing review
requests, not an approval signal. The main `reviewer_start_here.ordered_steps`
must contain only the active plan-review flow. Legacy provenance
reconfirmation, when present, belongs under `legacy_sidecar_workflows`; it is a
separate branch and its decisions must not be mixed into the main plan-review
decision CSV. If a supplied provenance sidecar has a different artifact date,
the handoff reports `provenance_reconfirmation_packet_date_mismatch`; if its
date cannot be verified, it reports
`provenance_reconfirmation_packet_date_unverified`. Handoff generation and
handoff snapshots also re-check embedded `packet_index.path` contracts when the
handoff claims the packet index exists or caches `packet_index.contract_ok=true`,
so stale cached values cannot hide a deleted, malformed, or drifted
packet-index artifact.
Workflow handoff generation uses the same artifact-directory sidecar resolution
for decision-sheet CSV/HTML paths and next-request HTML paths, so reviewer
start steps and regeneration commands do not depend on the current cwd.

Use the reviewer artifact staleness audit before handing checked-in or dated
reviewer artifacts to an operator, especially when generator guardrails changed
after the files were produced:

```powershell
python scripts\ncs_harness.py audit-aihr-reviewer-artifacts --artifact reports\aihr_plan_review_workflow_handoff_<DATE>.json --artifact reports\aihr_plan_review_packet_index_<DATE>.json --artifact reports\aihr_plan_review_packet_index_<DATE>.md --artifact reports\aihr_plan_review_packet_index_<DATE>.html --artifact reports\reviewer_entrypoint_<DATE>.md --out reports\aihr_reviewer_artifact_staleness_audit_<DATE>.json --markdown-out reports\aihr_reviewer_artifact_staleness_audit_<DATE>.md
```

In the managed Codex sandbox, direct shell execution of this command may fail
before dispatch because `ncs_harness.py` imports API modules at module load.
When sandboxed, rely on in-process regression coverage or workspace-local
`.venv` verification commands instead of escalating merely to prove this
read-only audit path.
If a workspace-local virtual environment is available, prefer it over the user
Anaconda interpreter for sandboxed verification so package imports stay inside
the workspace read-permission boundary.

The audit is report-only (`status_update_allowed=false`, `db_writes=false`,
`approval_claim=false`). It flags stale reviewer surfaces that expose
broad-command fields, show broad collection
commands, keep provenance reconfirmation in the main reviewer start flow, or
ship a packet-index JSON whose schema, `ok`, blocker count, link counts, or
linked artifact paths no longer satisfy the current reviewer contract.
Include the packet-index HTML artifact because workflow handoff points
operators there as the primary entrypoint; the HTML surface must carry the
same no-API/separate-workflow guardrails as Markdown.
The output JSON preserves exact forbidden-pattern values for auditability.
Markdown and CLI stdout intentionally avoid printing runnable collection
commands; stdout is a compact summary with counts, finding-code counts, and
artifact paths.

`export-aihr-plan-review-decision-sheet` writes blank decision columns, leaves
`source_packet` blank by default, and forces `status_update_allowed=false` for
every row. The active plan-review decision vocabulary is `include_in_draft`,
`exclude_from_draft`, or `defer`; these values shape draft review output only
and are not review-status approvals. If an export caller passes
`--source-packet`, the exporter must ignore it and report the ignored prefill
attempt; packet provenance belongs only to a completed human decision row.
`audit-aihr-plan-review-decisions` treats a pending row with non-blank
`source_packet` as invalid. After a human reviewer gives an explicit
`include_in_draft`, `exclude_from_draft`, or `defer` decision with a rationale,
use `record-aihr-plan-review-decision` to write a copied CSV only. The
`--source-packet` argument is required for that record step and must point to
the HTML/Markdown/JSON packet that the reviewer actually inspected, not the
request-order, decision-sheet, seedpack, packet-index, workflow-handoff, or
legacy provenance-reconfirmation artifact. If source packet, seedpack,
decision-sheet, or request-order artifact dates are visible, they must belong
to the same review family. The audit also rejects a packet whose
`source_request_order` is missing or points at a different request-order
artifact family when a request-order path can be resolved.
Pass `--request-order` to the record command when available; if omitted, the
recorder only uses the standard same-date decision-sheet sidecar/request-order
filename when it can resolve one locally. The decision audit applies the same
standard sidecar/request-order inference. Source-packet row matching compares
all populated decision-row identity fields and canonicalizes generated packet
forms such as list-valued `review_focus` and `review_question_source`; legacy
CSV rows without newer identity columns remain readable, but current rows bind
on the full populated identity.

```powershell
python scripts\ncs_harness.py record-aihr-plan-review-decision --decision-sheet reports\aihr_plan_review_decision_sheet_20260619.csv --request-order reports\aihr_plan_review_request_order_20260619.json --out-csv reports\aihr_plan_review_decision_sheet_recorded_20260619.csv --out reports\aihr_plan_review_decision_record_20260619.json --order 1 --decision include_in_draft --reason "<human rationale>" --reviewer-id "<reviewer>" --source-packet reports\aihr_plan_review_next_request_20260619.html
```

Re-run `audit-aihr-plan-review-decisions` on the copied CSV before considering
any later status import. A completed CSV decision is still not a DB review
status; it is only a packet-backed candidate for a future guarded import.

### Legacy Provenance Reconfirmation Audit

Use this chain when existing rows already carry a trusted-looking status such
as `human_reviewed`, `reviewed`, or `accepted` but the audit trail does not
contain packet-backed provenance. The reconfirmation packet and decision sheet
are review-only. They must not be treated as a status apply step.

Use the proofset command for release queue work. It regenerates the packet,
blank decision sheet, and decision audit from the same packet hash so operators
do not accidentally pair a fresh packet with a stale decision sheet or audit.

```powershell
python scripts\ncs_harness.py export-human-review-provenance-reconfirmation-proofset --out reports\human_review_provenance_reconfirmation_packet_<DATE>.json --markdown-out reports\human_review_provenance_reconfirmation_packet_<DATE>.md --html-out reports\human_review_provenance_reconfirmation_packet_<DATE>.html --decision-sheet-out reports\human_review_provenance_reconfirmation_decision_sheet_<DATE>.json --decision-sheet-csv-out reports\human_review_provenance_reconfirmation_decision_sheet_<DATE>.csv --decision-sheet-html-out reports\human_review_provenance_reconfirmation_decision_sheet_<DATE>.html --decision-sheet-markdown-out reports\human_review_provenance_reconfirmation_decision_sheet_<DATE>.md --decision-audit-out reports\human_review_provenance_reconfirmation_decision_audit_<DATE>.json --decision-audit-markdown-out reports\human_review_provenance_reconfirmation_decision_audit_<DATE>.md
```

The packet-only exporter and standalone decision-sheet/audit scripts are
diagnostic helpers. Automated agent queues must not run only
`export-human-review-provenance-reconfirmation-packet` for this blocker because
that can leave downstream proof artifacts stale.

The audit accepts only `reconfirm`, `downgrade_to_review_required`, or `defer`.
Rows with `reconfirm` or `downgrade_to_review_required` still require
`rationale`, `reviewer_id`, `reviewed_at`, `source_decision_packet`, and
`evidence_refs_json`. The audit writes `status_update_allowed=false`,
`db_writes=false`, `approval_claim=false`, and `guarded_apply_ready=false`.
It only separates pending, invalid, and action-eligible rows for a future
explicitly approved guarded apply design.

Use the AI-HR agent queue preflight before automated blocker work:

```powershell
python scripts\ncs_harness.py agent-queue-status --queue reports\aihr_agent_queue_20260617.json --out reports\aihr_agent_queue_status_20260617.json --markdown-out reports\aihr_agent_queue_status_20260617.md
```

The same status artifact is visible at `/aihr-agent-queue-status` and available
as JSON at `/api/aihr-agent-queue-status` after the dashboard starts.
If `reports\aihr_release_readiness_20260617.json` is newer, read its
`agent_work_queue_path` and pass that path to `--queue`.

Use `agent-queue-run-ready` only after the preflight passes. It executes only
items that the queue status marks `can_start_automated=true`, requires
`mutation_policy=regenerate_reports_only`, rejects shell metacharacters, and
does not run human-decision or guarded API collection items.

```powershell
python scripts\ncs_harness.py agent-queue-run-ready --queue reports\aihr_agent_queue_20260617.json --dry-run --out reports\aihr_agent_queue_run_dryrun_20260617.json --markdown-out reports\aihr_agent_queue_run_dryrun_20260617.md
python scripts\ncs_harness.py agent-queue-run-ready --queue reports\aihr_agent_queue_20260617.json --limit 1 --out reports\aihr_agent_queue_run_20260617.json --markdown-out reports\aihr_agent_queue_run_20260617.md
```

The latest run artifact is visible at `/aihr-agent-queue-run` and available as
JSON at `/api/aihr-agent-queue-run` after the dashboard starts.
The run JSON stores bounded stdout/stderr tails plus original/tail character
counts and truncation flags, not full command output.

### Read-Only Release Proof Refresh

Use this chain when refreshing release evidence during an overnight or handoff
session without changing source data, ontology preprocessing outputs, API
collection state, or trusted review statuses. Keep experimental refresh outputs
under `reports\overnight_sessions\readonly_refresh\` unless the operator
intentionally wants to replace the standard dashboard-discovered artifacts.
One intentional exception is an automated queue item whose command already
targets a standard dashboard artifact, such as
`export-ontology-definition-seedpack --out reports\aihr_ontology_definition_review_seedpack_*.jsonl`.
Running that queue item non-dry refreshes the standard seedpack by design; record
the run artifact and backlog source paths so the provenance is explicit. If a
fully isolated rehearsal is required, stop at the dry run or edit the queue copy
to point every expected artifact into `readonly_refresh`.

```powershell
python scripts\release_readiness_report.py --quality-report reports\aihr_quality_gates_with_transition_20260627_extra_safe.json --contract reports\mcp_tool_contract_20260627_extra.json --demo-json reports\aihr_plan_demo_20260627.json --demo-json reports\aihr_plan_demo_alias_20260627.json --demo-html reports\aihr_plan_demo_20260627.html --dashboard-verification reports\aihr_dashboard_surface_verification_20260627_extra_safe.json --review-priority-report reports\aihr_review_priority_20260627.json --out reports\overnight_sessions\readonly_refresh\aihr_release_readiness_20260627_extra_safe.json --markdown-out reports\overnight_sessions\readonly_refresh\aihr_release_readiness_20260627_extra_safe.md --agent-queue-out reports\overnight_sessions\readonly_refresh\aihr_agent_queue_20260627_extra_safe.json --agent-queue-markdown-out reports\overnight_sessions\readonly_refresh\aihr_agent_queue_20260627_extra_safe.md
python scripts\ncs_harness.py agent-queue-status --queue reports\overnight_sessions\readonly_refresh\aihr_agent_queue_20260627_extra_safe.json --out reports\overnight_sessions\readonly_refresh\aihr_agent_queue_status_20260627_extra_safe.json --markdown-out reports\overnight_sessions\readonly_refresh\aihr_agent_queue_status_20260627_extra_safe.md
python scripts\ncs_harness.py agent-queue-run-ready --queue reports\overnight_sessions\readonly_refresh\aihr_agent_queue_20260627_extra_safe.json --dry-run --out reports\overnight_sessions\readonly_refresh\aihr_agent_queue_run_dryrun_20260627_extra_safe.json --markdown-out reports\overnight_sessions\readonly_refresh\aihr_agent_queue_run_dryrun_20260627_extra_safe.md
```

Only run the non-dry `agent-queue-run-ready` command when the preflight is
`ok=true` and the selected items are `ready_to_start`,
`can_start_automated=true`, and `mutation_policy=regenerate_reports_only`.
This still must not run `collect-*`, `retry-*`, `preprocess-*`,
`review-training-transition-scenarios --apply`, guarded API collection items,
or any command that writes review statuses, including `candidate_auto`,
`human_reviewed`, `accepted`, or `reviewed`.

### Review Artifact Readability Audit

Use the readability audit before handing review packets, seedpacks, decision
sheets, or dashboard proof artifacts to an operator. It checks file-level UTF-8
readability, Korean mojibake markers, dense question-mark display noise, and
rows already flagged as `encoding_display_triage` or
`possible_encoding_or_display_noise`.

```powershell
python scripts\ncs_harness.py audit-review-artifact-readability `
  --reports-dir reports `
  --out reports\review_artifact_readability_audit_20260629.json `
  --markdown-out reports\review_artifact_readability_audit_20260629.md
```

For focused checks, pass explicit artifacts:

```powershell
python scripts\ncs_harness.py audit-review-artifact-readability `
  --artifact reports\human_review_backlog_report_20260627.json `
  --artifact reports\aihr_review_seedpack_blocker_ranked_20260629.jsonl `
  --out reports\review_artifact_readability_focused_20260629.json
```

This audit is report-only. A pass is not human approval and must not set
`human_reviewed`, `accepted`, `reviewed`, or `resolved`. Findings mean the
artifact should be regenerated, re-exported as UTF-8/UTF-8-SIG, or routed to
source display diagnostics before semantic review. Use `--strict` only when a
CI or release proof step should fail on readability findings.

Release-readiness can consume a readability audit with
`--review-readability-audit`. That integration does not treat a whole
`reports/` scan `ok=false` as a global ship/no-ship decision. It blocks only
when the audit contract is unsafe or when findings overlap the current release
proof artifacts or dashboard static artifacts supplied to the release report.
Historical review packets outside the active proof set remain advisory review
debt.

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

`generate-training-transition-eval-set` is report-only by default. Use `--apply`
only when an operator intentionally wants to insert or refresh `candidate_auto`
scenario rows. When `--reset-auto --apply` removes existing `candidate_auto`
rows before regeneration, that delete is also a DB mutation and is logged to
`review_audit_log` so recent-write audits can detect delete-only runs. The
command writes separate report sections for
`all_non_rejected`, `trusted_reviewed`, and `candidate_or_auto` so candidate
metrics are not mistaken for reviewed readiness. In JSON output, the mixed
summary is named `all_non_rejected_evaluation`; readiness automation should read
`evaluations.trusted_reviewed`.

`review-training-transition-scenarios --apply` also requires
`--allow-automated-status-write` before it may write non-trusted automated
candidate statuses. Trusted statuses still require the guarded human-decision
import path.
