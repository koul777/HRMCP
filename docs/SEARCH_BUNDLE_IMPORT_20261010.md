# Original search bundle import — 2026-10-10

The original web search implementation was available on the PC in
`C:/workspace/aside/.tmp/ncs-search-accuracy-d7a57e6.bundle`. The earlier
conclusion that the original files were absent was incorrect. This bundle
contains `d7a57e61ff89cb202c9530e20dce99aecbe5a13a`, based on `7ee69be`.
`git bundle verify` passed and the branch was imported without rewriting
published history.

The canonical and deployment `search/core.py` and `search/typo.py` files
match that original commit. The separately implemented PC spelling runtime
was removed. Deployment preflight, source preview and parity checks now
require the original typo module. The earlier PC implementation report is
marked historical; its measurements are not the active implementation's
results. The additional four-edit-family audit remains a separate diagnostic.

## Same-snapshot reproduction

Baseline and original bundle code were evaluated in separate processes on
`tmp/ncs_ontology_compact_fts_v2_nocol_20260929.db` in read-only mode.
The companion evidence JSON records DB, fixture, bundle and runtime hashes.
The source DB's size and modification time remained unchanged.

| Evaluation | Cases | Before Hit@3 | After Hit@3 | Before MRR | After MRR |
| --- | ---: | ---: | ---: | ---: | ---: |
| Original source-transposition typos | 120 | 56.7% | 96.7% | 0.4528 | 0.8875 |
| Regression | 40 | 100.0% | 100.0% | 0.9458 | 0.9458 |
| Development | 90 | 97.8% | 97.8% | 0.9102 | 0.9102 |
| Long-description development | 50 | 52.0% | 58.0% | 0.5071 | 0.5137 |

Every expected-code rank in regression40 and development90 is unchanged.
The typo inputs are identical before and after and cover all 24 NCS majors;
they are synthetic source self-retrieval, not independent semantic gold.
Exact names240 and codes240 each returned Hit@1 = Hit@3 = MRR = 1.0.
Long-description labor MRR declines from 0.7188 to 0.7125 and education MRR
from 0.5625 to 0.5583; these small regressions remain.

The previously exposed holdout v2 was measured once after importing the fixed
original runtime: 40 cases, Hit@1 95.0%, Hit@3 100.0%, MRR 0.9750.
This is not a fresh blind estimate. The operator-frozen v3 JSON is absent,
so a new blind holdout remains unmeasured.

## PC validation

The imported search workflow's checks plus deployment preflight and the
additional audit test ran 401 tests: 399 passed and the same two preexisting
real-DB routing failures remained. Those failures were also independently
reproduced on unchanged `7ee69be` during the earlier PC validation:

- `test_direct_unknown_explicit_job_request_fails_closed`: unresolved query
  subjects use the existing lexical fallback instead of returning
  `route_context_required`.
- `test_nonexact_hospitality_scope_cannot_run_unfiltered_mixed_search`: the
  existing fallback removes `params.job_scope` and the test raises `KeyError`.

The router and facade implementation were not changed by this import.
An additional 31 deployment source-boundary/preview tests passed, as did
repository lint and isolated smoke. Local command logs and JUnit evidence
are under `.state/search-bundle-import-20261010/`.

The bundle also supplies `.github/workflows/search-accuracy.yml`, which runs
search regression and deployment parity on Windows Python 3.11 and 3.12.
Remote CI results will be added after the merge commit is pushed and tested.
CI uses synthetic databases and cannot replace the real-snapshot measurements.

No source DB writes, human-review status changes, production deployment, or
credential output are part of this import.
