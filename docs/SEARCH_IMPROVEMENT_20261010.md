# HRMCP search continuation — 2026-10-10

The PC continuation starts from `7ee69be`. The web environment's unpushed
change files and original typo fixture were absent from this checkout and
GitHub. This is a separately implemented and measured continuation, not a
claim that the web changes or their 56.7% → 96.7% typo / 52% → 58% long-query
measurements were transferred.

## Changes

- Recover a unique single-edit official Hangul unit name before a broad
  compound fallback. Use the recovered name for the unit phrase tier and
  expose the original-to-official-name mapping in `unit_query_terms`.
- Reject ambiguous spelling guesses. Preserve exact names, codes, lexical
  prefixes, hard classification boundaries, and leaf query terms.
- Keep terms discarded by the long-query retrieval cap in the candidate-only
  task/KSA evidence pass. Increase the supporting evidence weight from 0.5 to
  1.0 only for long token-OR queries; stronger tiers and short queries retain
  their existing weights.
- Add an all-major spelling audit, synchronize deployment source mirrors,
  and require the spelling module in source preview/preflight checks.

## Same-database measurements

Both versions use the read-only 2026-09-29 compact snapshot
`tmp/ncs_ontology_compact_fts_v2_nocol_20260929.db`: 472,154,112 bytes and
13,435 units. The baseline source copy is under
`.state/search-improvement-20261010/baseline`. No operational DB or review
statuses were changed.

| Set | Cases | Before Hit@1 | After Hit@1 | Before Hit@3 | After Hit@3 | Before MRR | After MRR |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Regression | 40 | 90.0% | 90.0% | 100.0% | 100.0% | 0.9458 | 0.9458 |
| Development | 90 | 85.6% | 86.7% | 97.8% | 97.8% | 0.9102 | 0.9157 |
| Long-query development | 50 | 42.0% | 46.0% | 52.0% | 56.0% | 0.5071 | 0.5411 |
| New all-major synthetic typos | 120 | 2.5% | 98.3% | 4.2% | 98.3% | 0.0319 | 0.9833 |

The typo pool rotates substitution, deletion, insertion and transposition
across five deterministic official-name samples from each of 24 majors.
Two ambiguous cases remain uncorrected. This pool differs from the web
environment's original 120 cases. It measures source self-retrieval and is
not human-validated relevance or a blind semantic holdout.

Exact official-name and code audits each sampled 240 queries across all 24
majors and returned Hit@1 = Hit@3 = MRR = 1.0. Regression query ranks are
unchanged. Development has no rank regressions and one rank improvement.
Long-query MRR still dips slightly in 노무 (0.7188 → 0.7125) and 교육
(0.5625 → 0.5563); the global gain does not erase those category limits.

## Validation and limits

Lint and smoke passed. The final search/deployment checks and complete
unittest shard results are recorded in the companion evidence JSON and
`.state/search-improvement-20261010/` command logs.

Two existing real-DB routing tests also fail on the unchanged `7ee69be`
source copy:

- `ExplicitJobScopeRealDbRegressionTests.test_direct_unknown_explicit_job_request_fails_closed`
  expects `route_context_required`; the existing facade instead searches the
  unresolved framed subject lexically and returns loose `Function` matches.
- `ExplicitJobScopeRealDbRegressionTests.test_nonexact_hospitality_scope_cannot_run_unfiltered_mixed_search`
  expects `params.job_scope`, which the existing lexical-fallback route removes;
  the test raises `KeyError: 'job_scope'`.

These expectations conflict with the existing unresolved-query fallback and
remain unresolved here. They are not passing checks or new spelling failures.
The source/deploy mirror and cold-versus-warm SQL-count failures discovered
during validation were corrected and checked again.

The fresh blind holdout remains unmeasured: the operator-frozen
`tests/fixtures/ncs_search_eval_nl_holdout_v3.json` does not exist. Its README
requires operator review and freezing; no human review or freeze was inferred
from the request. Neither the development gains nor the synthetic typo audit
establish fresh semantic generalization.
