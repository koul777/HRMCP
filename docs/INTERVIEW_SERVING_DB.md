# Interview Serving DB

This project should keep the full NCS source database out of Git and serve only
the smaller read-only slice needed by interview-question applications.

## Architecture

- `data/processed/ncs.db` is the canonical local build artifact. It can be very
  large and must not be committed.
- `scripts/export_interview_serving_db.py` creates a derived SQLite artifact for
  interview serving.
- The derived DB keeps only the tables needed by the MCP interview path:
  `classifications`, `competency_units`, `competency_elements`,
  `performance_criteria`, `ksa_items`, `ncs_training_courses`, and
  `ncs_query_aliases`.
- The interview application should call NCS_MCP over MCP/HTTP and should not
  bundle `NCS_DB.xlsx` or the full `ncs.db`.
- The deployed MCP process mounts the derived DB and opens it read-only through
  `NCS_DB_PATH`.

## Exact export command

Run from `C:\workspace\NCS_MCP`:

```powershell
python scripts\export_interview_serving_db.py `
  --source data\processed\ncs.db `
  --destination tmp\ncs_interview_serving.db `
  --report reports\ncs_interview_serving.json
```

Use the generated report as the release manifest for table counts and file size.

## GitHub Release / Artifact policy

- Commit source code, docs, scripts, tests, and small manifests only.
- Do not commit `.db`, `.db-wal`, `.db-shm`, `.xlsx`, or generated `tmp/` files.
- Publish `tmp\ncs_interview_serving.db` as a GitHub Release asset for stable
  deployment, or as a GitHub Actions artifact for short-lived CI verification.
- Name release assets with an explicit build date or NCS source version, for
  example `ncs_interview_serving_2026-07-23.db`.
- Keep the JSON report next to the release asset so consumers can verify the
  expected tables before deployment.

## Deployment environment

Set the DB path in the MCP runtime:

```powershell
$env:NCS_DB_PATH = "C:\data\ncs_interview_serving.db"
$env:NCS_MCP_READ_ONLY = "1"
```

For container deployment, mount the DB file and set:

```text
NCS_DB_PATH=/data/ncs_interview_serving.db
NCS_MCP_READ_ONLY=1
```

The interview application then points at the MCP endpoint, not at a local DB:

```text
NCS_MCP_URL=https://<your-ncs-mcp-host>/mcp
```

## Read-only operation

The serving artifact is derived data. Runtime code should treat it as read-only:

- open the source/export inputs in read-only mode where possible;
- avoid migrations or write-time cache tables in the deployed DB;
- rebuild by rerunning the export command from the canonical `ncs.db`;
- replace the mounted DB atomically during deployment instead of mutating it.

## Current size/count evidence

Observed on 2026-07-23 from
`C:\workspace\NCS_MCP\tmp\ncs_interview_serving_test.db`:

| Evidence | Value |
| --- | ---: |
| File size | 117,108,736 bytes |
| `classifications` | 1,109 |
| `competency_units` | 13,435 |
| `competency_elements` | 47,620 |
| `performance_criteria` | 196,658 |
| `ksa_items` | 574,279 |
| `ncs_training_courses` | 11,819 |
| `ncs_query_aliases` | 32 |

The previous generated report at
`reports\ncs_interview_serving_test.json` records the same table counts and
`purpose: read-only interview MCP serving DB`.

## Release verification checklist

Before publishing a DB asset:

1. Run the export command and keep the JSON report.
2. Confirm the DB file exists and is not empty.
3. Open the DB in read-only mode and verify the seven table counts above.
4. Start NCS_MCP with `NCS_DB_PATH` pointing to the derived DB.
5. Smoke-test `ncs_search` for a known 세분류/능력단위 query.
6. Smoke-test `ncs_unit_detail` with `include=["elements","criteria","ksa"]`.
7. Confirm the interview app receives KSA through `NCS_MCP_URL`.
8. Upload the `.db` and `.json` report as Release assets or CI artifacts.
9. Confirm no `.db`, `.xlsx`, or `tmp/` generated files are staged for Git.
