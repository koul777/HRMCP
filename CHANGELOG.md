# Changelog

## Unreleased

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

