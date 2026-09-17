# Changelog

## Unreleased

- Restored CI, which had failed on every push since 2026-09-12. Builder release
  guards now accept a Windows 8.3 short spelling of the Builder root (GitHub
  runners hand out `C:\Users\RUNNER~1\...`) while still resolving the full
  path and checking every component below the root for reparse points, and
  prebuilt-output evidence compares its recorded location canonically instead
  of by string. Refresh baseline promotion now detects junctions on Python 3.11,
  where `Path.is_junction()` does not exist. Tests that need the canonical
  12 GB database skip when it is absent, and later CI steps (lint, smoke,
  STDIO/HTTP) still run after a unit-test failure.
- Escaped LIKE wildcards in the public `ncs_training` search path. A user's `%`
  or `_` is now matched literally, as `ncs_search` already did, so a query such
  as `100%` finds the course whose name contains it instead of matching every
  row. An AST audit of all 803 f-string SQL sites found no injection path: user
  values are always bound and every SQL fragment comes from fixed server-side
  text (`reports/sql_injection_audit_20260918.md`).
- Split CI unit tests into three parallel shards (`scripts/run_test_shard.py`).
  Each test module lands in exactly one shard by CRC32 of its name, and a
  module that fails to import always stays in shard 0. The serial step took
  about 38 minutes on a hosted Windows runner; by the measured per-module time
  the largest shard is about 16 minutes.
- Added an unexposed 40-query search holdout (`ncs_search_eval_nl_holdout_v2.json`),
  committed before its single measurement. It shares no query text with the
  40-query regression set or the 51-query holdout, which was re-measured across
  tuning stages and no longer generalizes. First measurement: Hit@1 0.450,
  Hit@3 0.500, MRR 0.483. Cases on units no earlier set expects hit 4 of 14,
  against 16 of 26 on previously used units, so the in-sample 0.875 and the old
  holdout's 0.608 overstate generalization. Do not tune against this set.
- Carry an explicit classification filter through `ncs_discover_tools` as well
  as `ncs_execute_tool`, so agent routing and execution preserve the same NCS
  scope without inferring a major from query text.
- Compute fallback IDF within an explicitly filtered classification corpus, so
  scope-aware search ranking does not inherit token frequencies from unrelated
  NCS majors.

- Carried caller-supplied `classification_filter` through the query route and
  `ncs_execute_tool` path into `ncs_search`, preserving the route fingerprint
  while enforcing the same parameter-bound NCS scope at execution time. This
  lets an HR or education-planning client constrain ambiguous terms such as
  vehicle dispatch or event preparation without adding holdout-specific aliases.
- Weighted fallback search tokens by document frequency instead of a hand kept
  generic-word list, so a token naming few units outranks one spread across the
  catalogue. Holdout Hit@3 0.451 -> 0.549 with the 40-query regression set
  unchanged, 5 queries newly passing and none newly failing. Document
  frequencies are counted in one pass, which halves the cost on the compact
  serving profile that ships without the unit_name_raw index.
- Added a 51-query independent search holdout, authored from official NCS unit
  definitions rather than the tuned alias list, and froze the existing 40-query
  set as a regression set. The holdout puts generalization at Hit@3 0.451
  against the in-sample 0.875, and shows the alias layer is phrase-brittle.
- Ranked a shared competency unit's home classification above a borrowed copy,
  so `사옥 보안 점검` returns 총무보안관리 under 총무 instead of the older
  자원봉사관리 copy. Raised 40-query Hit@1 from 0.825 to 0.850 and MRR from
  0.8550 to 0.8675.
- Played the promo video inline in the README, which GitHub had been serving as
  a downloadable mp4, and reordered the header to title, summary, video, poster.
- Improved Korean natural-language NCS search fallback ranking with field-aware
  token scoring, generic-term down-weighting, and HR recall regression coverage.
- Added a 40-query HR search evaluation set, Hit@1/Hit@3/MRR audit tooling, and
  an initially non-blocking CI Hit@3 quality gate.
- Raised the 40-query natural-language search Hit@3 from 0.500 to 0.875 with
  scored, high-specificity practitioner-to-NCS intent aliases and cross-domain
  ambiguity guards.
- Added a refreshed HRMCP overview poster and reorganized the README around the
  five primary HR workflows, evidence flow, and copy-ready example prompts.
- Rendered missing competency-element levels as `-` without modifying source
  values, and separated PDF/OCR ingestion packages plus pytest into optional
  dependency groups.
- Documented the open-source release quick start, runtime scope, and API key
  handling expectations.
- Added a release-readable README for the NCS-centered MCP surface.
## 2026-09-11

- Increased fallback search weighting for `competency_units.api_definition` so
  concrete task evidence can outrank a name-only candidate while keeping unit
  names as the strongest field. Added a regression test and kept the source and
  Vercel mirror byte-identical.
- Added a bounded second-stage task/KSA evidence reranker for the weakest
  `token_or` unit fallback. It only inspects already retrieved candidates and
  applies a boost when at least two independent query tokens occur in attached
  criteria/KSA evidence, avoiding full-corpus scans and single-word noise.
  Canonical holdout Hit@3 improved from `0.5490` to `0.5882` (28 -> 30 of 51)
  while the 40-query development Hit@1 stayed at `0.7750`.
