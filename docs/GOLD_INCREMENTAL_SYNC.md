# Gold LPG Incremental Synchronization

`gold_incremental` compares two completed, canonical serving-core Gold NDJSON
exports. It does not connect to Neo4j, open the NCS source database, or apply
any graph mutation.

```powershell
# Default: validate both exports and print a dry-run report only.
python scripts\diff_gold_lpg.py --previous exports\gold-before.ndjson --current exports\gold-after.ndjson

# Retain a deterministic operation plan for a later loader.
python scripts\diff_gold_lpg.py --previous exports\gold-before.ndjson --current exports\gold-after.ndjson --out exports\gold-incremental-plan.ndjson
```

The plan has four operation record types:

- `upsert_node`
- `upsert_relationship`
- `tombstone_relationship`
- `tombstone_node`

Every tombstone carries `requires_operator_approval: true` and
`action: plan_only_no_apply`. No command in this repository can apply one. A
future Neo4j loader must independently require an operator-approved deletion
stage, apply relationship tombstones before node tombstones, and retain this
plan manifest as audit evidence.

Both input streams must have a valid final Gold manifest, matching
`records_sha256`, matching graph counts, the same serving profile, and the same
projection schema. IDs are indexed in a temporary SQLite database; source rows
and source NDJSON files are read only. The output is atomically replaced only
after all validations and writes succeed.
