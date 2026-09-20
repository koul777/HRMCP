# Holdout v3 (planned frozen set)

## Status

- **Not measured yet.** This README is a contract stub for the next release gate.
- Holdout v2 (`ncs_search_eval_nl_holdout_v2.json`) remains the last frozen
  generalization estimate, but its miss queries are now visible in reports and
  must not be used for alias/intent tuning.

## Contract

1. 60 queries, balanced across 인사/노무/교육/총무/회계 (and optional 비HR
   controls if the gate needs cross-domain protection).
2. Expected unit codes verified in `data/processed/ncs.db` before freeze.
3. Public reports may publish **aggregate** Hit@1 / Hit@3 / MRR and category
   tables only. Do not print individual queries or top-3 lists in Markdown.
4. Measure at most once per release candidate. Never add aliases that quote the
   fixture text.
5. File path when frozen: `tests/fixtures/ncs_search_eval_nl_holdout_v3.json`
   (absent until an operator freezes the set).

## Generator

Use `scripts/prepare_ncs_search_holdout_v3_candidates.py` to build a private
candidate pool that excludes v1/v2/dev/regression queries. The generator does
not freeze or measure the set by itself.
