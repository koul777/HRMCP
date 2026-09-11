# Changelog

## Unreleased

- Improved Korean natural-language NCS search fallback ranking with field-aware
  token scoring, generic-term down-weighting, and HR recall regression coverage.
- Added a 40-query HR search evaluation set, Hit@1/Hit@3/MRR audit tooling, and
  an initially non-blocking CI Hit@3 quality gate.
- Rendered missing competency-element levels as `-` without modifying source
  values, and separated PDF/OCR ingestion packages plus pytest into optional
  dependency groups.
- Documented the open-source release quick start, runtime scope, and API key
  handling expectations.
- Added a release-readable README for the NCS-centered MCP surface.

