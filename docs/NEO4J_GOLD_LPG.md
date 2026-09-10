# Neo4j Gold LPG offline export

## Boundary and safety contract

`src/ncs_mcp/gold_lpg.py` is the read-only projection boundary between the
prepared NCS SQLite authority and the optional Neo4j Gold LPG read model. The
standalone `scripts/export_gold_lpg.py` command only reads SQLite and emits
JSON. The separate `scripts/load_gold_lpg_neo4j.py` command is dry-run by
default; it reads the narrow `NCS_MCP_GOLD_*` environment only after an
operator explicitly supplies `--apply`.

SQLite is opened with `mode=ro` and `PRAGMA query_only = ON`. The exporter never
initializes schemas, changes raw rows, updates review statuses, or writes a
SQLite sidecar intentionally. In particular, `ksa_items.ksa_text_raw` remains
immutable, and automated data must not become `human_reviewed`, `accepted`, or
`reviewed` through this path.

The safe default is a count-only readiness dry run. It calls
`preflight_gold_projection`, prints a deterministic
`ncs_gold_projection_readiness_v1` summary to stdout, creates no artifact, and
does **not** call the list-building `build_gold_lpg_projection`:

```powershell
python scripts\export_gold_lpg.py --db data\processed\ncs.db
```

`--manifest-out` alone writes that same count-only readiness manifest and still
does not build the full graph:

```powershell
python scripts\export_gold_lpg.py `
  --db data\processed\ncs.db `
  --manifest-out exports\ncs_gold_lpg.readiness.json
```

Writing the serving-core projection requires an explicit `--out` destination:

```powershell
python scripts\export_gold_lpg.py `
  --db data\processed\ncs.db `
  --out exports\ncs_gold_lpg.json `
  --manifest-out exports\ncs_gold_lpg.manifest.json
```

Every invocation passes the exact singleton `SERVING_CORE_PROFILE` to both the
count-only preflight and, when allowed, `build_gold_lpg_projection`. The profile
keeps the serving path compact: a performance criterion also carries the
`Task` label, element-to-concept evidence is summarized, and detailed
`task_ksa_concept_relations` are excluded. `--out` proceeds only when
`in_memory_export_allowed=true`. Otherwise it exits with a
`streaming_required` error before calling the projector and writes no output or
manifest artifact. There is deliberately no force/large-memory override. Large
sources use the separately bounded NDJSON exporter:

```powershell
python scripts\export_gold_lpg_stream.py `
  --db data\processed\ncs.db `
  --out exports\ncs_gold_lpg.ndjson `
  --batch-size 10000
```

Readiness and streaming manifests expose
`scope_contract.schema=ncs_gold_serving_core_scope_v1`. It states that this is
a hybrid serving-core projection, not a complete SQLite ontology replica.
Cross-concept relations, task similarity, career paths, qualifications, and
job-base evidence remain available through the authoritative SQLite MCP and
are listed with table presence and row counts as
`sqlite_authoritative_fallback` evidence.

The streaming command accepts only the canonical `SERVING_CORE_PROFILE`, reads
SQLite with `fetchmany`, writes through a same-directory temporary file, and
atomically replaces the destination only after success. It rejects the source
database and its `-wal`, `-shm`, and `-journal` sidecars as destinations. The
maximum batch size is 100,000; there is no full-fidelity or force flag.
All projection queries run inside one explicit read transaction; the final
manifest records the transaction-snapshot flag plus SQLite schema/data
versions. The temporary file is flushed and `fsync`ed before replacement.

On the 2026-09-10 prepared database, the readiness pass estimates 875,200
nodes and 4,289,709 relationships (5,164,909 records total), including 389,481
direct job-to-KSA summary edges. The serialized payload estimate is about
3.49 GB and the conservative in-memory peak is about 10.47 GB, so the JSON
projector is correctly refused for this source. The serving profile omits
14,475,815 detailed task-KSA rows and 401,294 inherited course-concept links;
their authoritative source rows are not deleted.

Both files use UTF-8, sorted JSON keys, two-space indentation, a trailing
newline, and a same-directory temporary-file plus atomic replace. `--out` and
`--manifest-out` cannot overwrite the source DB, its `-wal`, `-shm`, or
`-journal` sidecars, or each other. The full
serving-core projection is currently assembled in memory before serialization; a production
DB can require substantial RAM. The readiness manifest estimates nodes, edges,
serialized size, and amplified peak memory without reading source rows into
Python lists. A high-risk result requires a separately reviewed streaming or
partitioned exporter.

## Projection schema

The count-only CLI summary uses `ncs_gold_projection_readiness_v1` and contains
the selected `serving_core` profile, thresholds, table counts, size estimates,
source fingerprint, risk reasons, recommended execution mode, and
`in_memory_export_allowed`. When `--out` succeeds, stdout and
`--manifest-out` also include `projection_manifest`.

The serving-core `--out` artifact schema is `ncs_gold_lpg_projection_v2`:

```json
{
  "schema": "ncs_gold_lpg_projection_v2",
  "profile": {
    "name": "serving_core",
    "include_classifications": true,
    "include_competency_units": true,
    "include_competency_elements": true,
    "include_performance_criteria": true,
    "include_ontology_concepts": true,
    "include_criteria_concept_links": true,
    "include_element_concept_summary": true,
    "include_task_ksa_detailed_relations": false,
    "include_training_courses": true,
    "include_training_unit_links": true,
    "include_training_concept_links": true,
    "include_training_element_links": true,
    "include_training_goal_links": true,
    "include_training_delivery": true
  },
  "fingerprint": "<sha256>",
  "nodes": [],
  "edges": [],
  "diagnostics": [],
  "manifest": {
    "schema": "ncs_gold_lpg_projection_v2",
    "profile": {
      "name": "serving_core",
      "include_classifications": true,
      "include_competency_units": true,
      "include_competency_elements": true,
      "include_performance_criteria": true,
      "include_ontology_concepts": true,
      "include_criteria_concept_links": true,
      "include_element_concept_summary": true,
      "include_task_ksa_detailed_relations": false,
      "include_training_courses": true,
      "include_training_unit_links": true,
      "include_training_concept_links": true,
      "include_training_element_links": true,
      "include_training_goal_links": true,
      "include_training_delivery": true
    },
    "fingerprint": "<same sha256>",
    "projection_fingerprint": "<same sha256>",
    "node_count": 0,
    "edge_count": 0,
    "node_counts": {},
    "edge_counts": {},
    "diagnostic_count": 0,
    "omitted_diagnostics": [],
    "summarized": {},
    "source_tables": [],
    "read_only": true,
    "db_writes": false,
    "approval_claim": false
  },
  "read_only": true,
  "db_writes": false,
  "approval_claim": false
}
```

Every node has `id`, `labels`, `properties`, and `provenance`; every edge has
`id`, `type`, `source`, `target`, `properties`, and `provenance`. IDs use
`ncs:<entity-type>:<escaped-source-key>`. Provenance retains `source_table`,
`source_key`, and the source review/link status when present. The fingerprint
is SHA-256 over canonical sorted nodes and edges and intentionally excludes file
paths and timestamps.

The projected path and fixed relationship types are:

```text
NCSJobCategory -[:HAS_SUB_CATEGORY]-> NCSJobCategory
NCSJobCategory -[:HAS_SUB_CATEGORY]-> NCSJob
NCSJob -[:REQUIRES_UNIT]-> CompetencyUnit
NCSJob -[:REQUIRES_KNOWLEDGE|REQUIRES_SKILL|REQUIRES_ATTITUDE|REQUIRES_KSA]-> KSAConcept
CompetencyUnit -[:DEFINED_BY]-> PerformanceElement
PerformanceElement -[:HAS_CRITERION]-> PerformanceCriterion:Task
PerformanceCriterion:Task|PerformanceElement
  -[:REQUIRES_KNOWLEDGE|REQUIRES_SKILL|REQUIRES_ATTITUDE|REQUIRES_KSA]-> KSAConcept
TrainingCourse -[:COURSE_COVERS_UNIT]-> CompetencyUnit
TrainingCourse -[:COURSE_COVERS_CONCEPT|COURSE_GOAL_COVERS_CONCEPT]-> KSAConcept
TrainingCourse -[:COURSE_COVERS_ELEMENT]-> PerformanceElement
TrainingCourse -[:COURSE_HAS_DELIVERY]-> TrainingDelivery
InternalJobRole -[:ALIGNED_TO]-> NCSJob
```

The direct NCSJob-to-KSA relationships are serving summaries derived from the
criterion links. They retain source-link, distinct unit/element/criterion,
status, method, and bounded sample metadata. Consequently
`InternalJobRole -> NCSJob -> KSAConcept` is a real two-hop serving path; the
four-hop unit/element path remains available when detailed task context is
needed.

`HAS_NCS_JOB`, `HAS_ELEMENT`, `REPRESENTS_TASK`, and detailed
`TASK_KSA_CONCEPT_RELATION` records belong to the library's separate
full-fidelity profile and are not emitted by this CLI. The CLI intentionally
has no profile-selection or large-memory override, so readiness estimates and
the artifact cannot drift onto different graph shapes.

The ontology is therefore not omitted: `KSAConcept` nodes, direct
criterion/task-to-KSA evidence, element-to-KSA summaries, and job-to-KSA
summaries are in the serving graph. The 3.2M `ontology_concept_relations` rows
remain candidate-state evidence and are not asserted wholesale as approved
facts. `ncs_mcp.gold_deep_evidence` retrieves them as a bounded second stage
(at most 25 selected concepts and 200 relations). When task/atomic provenance
is required, its criteria-indexed hydration path reads the omitted
`task_ksa_concept_relations` table using at most 25 criterion IDs and 200 rows.
Both paths are SQLite read-only, preserve source review status, and return
`approval_claim=false`; neither is currently added to the public MCP tool
registry.

`build_gold_lpg_projection` and the production streaming exporter accept
optional, validated `internal_roles` and `role_alignments` iterables. This is
an adapter for tenant-scoped
`InternalJobRole` nodes and candidate `ALIGNED_TO` edges; unresolved candidates
remain diagnostics rather than asserted edges. The streaming CLI accepts
explicit `--internal-roles` and `--role-alignments` JSON/JSONL sources, does not
invent tenant roles, rejects personal/employee fields and trusted review
statuses, and never emits a dangling alignment edge.
Missing optional source tables produce empty groups. A concept definition is exported
as semantic text only when it is non-boilerplate, `definition_status=defined`,
and carries a trusted human status. Candidate or boilerplate definitions remain
unpromoted.

## Staged Neo4j setup

1. Freeze a verified SQLite source copy and run the count-only readiness command.
2. For a production-size source, export NDJSON with
   `scripts/export_gold_lpg_stream.py`; retain its final manifest and
   `records_sha256` with the batch. The in-memory JSON command remains for small
   fixtures only.
3. Provision a separate Neo4j database. The guarded loader applies the fixed
   `NEO4J_SCHEMA_DDL` itself unless `--no-schema` is explicitly selected.
4. Validate first, then explicitly load nodes before edges in bounded batches:

   ```powershell
   python scripts\load_gold_lpg_neo4j.py --ndjson exports\ncs_gold_lpg.ndjson
   python scripts\load_gold_lpg_neo4j.py --ndjson exports\ncs_gold_lpg.ndjson `
     --apply --checkpoint reports\gold-load-checkpoint.json --reconcile
   ```

   The first command is network-free dry-run. `--apply` requires the narrow
   `NCS_MCP_GOLD_ENABLED`, `URI`, `USERNAME`, `PASSWORD`, and `DATABASE`
   environment contract. Errors never echo their values.
5. Compare Neo4j label/type counts with `manifest.node_counts` and
   `manifest.edge_counts`, then sample provenance paths back to SQLite.
6. Only after structural acceptance, generate embeddings, set vector
   properties, create vector indexes, and validate retrieval quality.

The import templates are fixed and parameterized; callers do not interpolate
labels, relationship types, IDs, or property values:

```cypher
UNWIND $nodes AS row
MERGE (node:LpgNode {id: row.id})
SET node = row.properties,
    node.labels = row.labels,
    node.source_table = row.provenance.source_table,
    node.source_key = row.provenance.source_key,
    node.source_review_status = row.provenance.review_status
SET node:PerformanceCriterion:Task
```

```cypher
UNWIND $edges AS row
MATCH (source:LpgNode {id: row.source})
MATCH (target:LpgNode {id: row.target})
MERGE (source)-[edge:HAS_CRITERION {id: row.id}]->(target)
SET edge = row.properties,
    edge.edge_type = row.type,
    edge.source_table = row.provenance.source_table,
    edge.source_key = row.provenance.source_key,
    edge.source_review_status = row.provenance.review_status
```

The base schema creates a unique `LpgNode.id` constraint, an index on
`LpgNode.node_type`, and one unique `edge.id` constraint for every allowlisted
relationship type. `MERGE` makes a repeated batch idempotent for projected IDs;
it is not a live deletion/reconciliation protocol. A later source deletion will
not delete its old Neo4j record automatically.

`scripts/diff_gold_lpg.py` compares canonical previous/current streams through
a temporary disk-backed index and emits deterministic Upserts plus tombstone
plans. Tombstones are relationship-first and always carry an operator-approval
gate; the current loader deliberately cannot apply them.

## Embedding and vector configuration

The graph export contains text evidence but does not generate embeddings. The
provider-neutral `ncs_mcp.embedding_batches` module now produces bounded,
deterministic work batches for `PerformanceCriterion`, `PerformanceElement`,
and `KSAConcept`, and validates returned vectors into write-free patch records.
It performs no network or database write and intentionally supplies no default
embedding vendor. Before an
embedding run, record an immutable model identifier/revision and its exact
output dimension in the batch metadata, for example:

```text
EMBEDDING_MODEL_ID=<provider/model@revision>
EMBEDDING_DIMENSIONS=<positive integer reported by that model>
```

The optional local pipeline is explicit and local-cache-only by default:

```powershell
python scripts\export_gold_embeddings.py --db data\processed\ncs.db `
  --out exports\ncs_gold_embeddings.ndjson `
  --model Qwen/Qwen3-Embedding-0.6B --max-records 100
```

Omit `--max-records` only for a planned full run. `--allow-download` is the
separate network opt-in. Patch artifacts contain vectors and reproducibility
metadata but never the semantic input text. `apply_embedding_patches()` in
`ncs_mcp.neo4j_loader` only permits three labels and a fixed property set.
The Data Builder Gold tab exposes the same bounded generation, dry-run, and
guarded apply sequence; apply is blocked until the same Builder version's
graph load has passed count reconciliation.

Validate patches and fixed vector-index DDL without connecting to Neo4j:

```powershell
python scripts\load_gold_embeddings_neo4j.py `
  --patches exports\ncs_gold_embeddings.ndjson --create-indexes `
  --out reports\gold_embedding_load.json
```

After the graph nodes exist, an operator can explicitly apply both the fixed
embedding properties and the three cosine indexes by adding `--apply`.

## Internal-role candidate overlay

Organization roles stay tenant-scoped external inputs. Generate a read-only,
candidate-only packet against the complete NCS catalog:

```powershell
python scripts\map_internal_roles.py --db data\processed\ncs.db `
  --roles <internal-roles.json> `
  --out <internal-role-mapping-packet.json>
```

The Data Builder Gold tab accepts this JSON packet. It verifies role identity,
rejects personal/employee fields, records a canonical overlay digest, and emits
role nodes before eligible `ALIGNED_TO` edges. `candidate` and
`review_required` may become edges; `ambiguous` and `unresolved` remain review
metadata and never become approval claims.

MCP exposes the active vocabulary and secret-free readiness as
`ontology://gold/schema` and `ncs://gold/status`. Existing `ncs_analysis` adds
`mode=internal_role` and `mode=semantic`; disabled or failed Gold returns a
bounded unavailable context while the SQLite MCP remains authoritative.
An internal-role request that explicitly asks for education or training keeps
the role context first, then executes a bounded, read-only
`recommend_training_for_task` chain for the aligned NCS job/unit evidence.

Use the same model, text construction, normalization policy, and dimension for
both indexed records and query vectors. Do not silently change or truncate
dimensions. Populate only these currently allowlisted properties:

- `(:PerformanceCriterion).embedding`
- `(:PerformanceElement).embedding`
- `(:KSAConcept).embedding`

Generate the matching cosine index DDL from the validated integer dimension:

```powershell
$env:PYTHONPATH = "C:\workspace\NCS_MCP\src"
python -c "from ncs_mcp.gold_lpg import neo4j_vector_index_ddl; print(*neo4j_vector_index_ddl(1536), sep='\n')"
```

Replace `1536` with the configured model dimension. The generated indexes are
`ncs_lpg_performance_criterion_embedding` and
`ncs_lpg_performance_element_embedding`, and
`ncs_lpg_ksa_concept_embedding`. Confirm `ONLINE` state with
`SHOW VECTOR INDEXES` before querying.

For Neo4j 2026.01 and later using Cypher 25, prefer `SEARCH`:

```cypher
MATCH (criterion:PerformanceCriterion)
  SEARCH criterion IN (
    VECTOR INDEX ncs_lpg_performance_criterion_embedding
    FOR $queryVector
    LIMIT $topK
  ) SCORE AS score
RETURN criterion.id, criterion.text, score
ORDER BY score DESC
```

For earlier compatible Neo4j releases, the procedure form remains a fallback:

```cypher
CALL db.index.vector.queryNodes(
  'ncs_lpg_performance_criterion_embedding', $topK, $queryVector
) YIELD node, score
RETURN node.id, node.text, score
ORDER BY score DESC
```

Neo4j documents `SEARCH` as the preferred surface from 2026.01 and deprecates
`db.index.vector.queryNodes()` in 2026.04. Keep the procedure only for tested
server-version compatibility. See the official
[SEARCH clause](https://neo4j.com/docs/cypher-manual/current/clauses/search/)
and [vector index](https://neo4j.com/docs/cypher-manual/current/indexes/semantic-indexes/vector-indexes/)
documentation.

## Fallback and acceptance

Neo4j is an optional serving/read-model experiment, not the source of truth.
If export, load, count reconciliation, provenance sampling, or vector-quality
checks fail, stop the Gold load and continue using the prepared SQLite MCP.
Regenerate from SQLite after fixing the projection or loader; do not edit raw
NCS source fields or trusted review status to make a graph load pass.

Run the offline acceptance checks without the real DB:

```powershell
$env:PYTHONPATH = "C:\workspace\NCS_MCP\src"
python -m unittest tests.test_gold_lpg tests.test_export_gold_lpg -v
python -m py_compile scripts\export_gold_lpg.py
```

For an operator-approved representative copy, acceptance additionally requires:

- default execution creates no artifact and leaves the SQLite SHA-256 unchanged;
- repeated exports have the same manifest fingerprint and byte-identical
  manifest JSON;
- JSON node/edge counts match the Neo4j post-load counts;
- sampled node and edge provenance resolves to the recorded SQLite source key;
- no review status is promoted and no candidate/boilerplate definition is
  presented as human-approved;
- vector dimensions match the recorded model, all three indexes are `ONLINE`, and
  retrieval uses `SEARCH` where supported;
- rollback is documented as discarding/rebuilding the Neo4j read model while
  retaining SQLite as authoritative.

## Local Builder and MCP launcher

For the fixed local Docker container `ncs-mcp-neo4j-gold`, the local launcher
can derive the narrow `NCS_MCP_GOLD_*` child environment from `docker inspect`
without writing credentials to an env file or printing their values. Its
default is a sanitized, non-launching status check:

```powershell
python scripts\start_local_gold.py
python scripts\start_local_gold.py --target health
python scripts\start_local_gold.py --target probe
```

`probe` opens the production Gold runtime and performs one fixed, bounded,
read-only query. MCP and Builder targets are also dry runs unless `--launch` is
explicitly supplied:

```powershell
python scripts\start_local_gold.py --target mcp
python scripts\start_local_gold.py --target mcp --launch
python scripts\start_local_gold.py --target mcp --transport streamable-http --mcp-port 8000 --launch
python scripts\start_local_gold.py --target builder --launch
```

Container name, target, transport, dimensions, ports, timeouts, Docker input
variables, and child Gold variables are validated or allowlisted. Bolt must be
published specifically on `127.0.0.1` or `::1`; `0.0.0.0`, `::`, remote-only,
stopped, unhealthy, malformed, or unreachable configurations fail closed.
Children run with Python isolated mode (`-I`) against trusted absolute project
scripts. All inherited `PYTHON*` variables and every non-allowlisted
`NCS_MCP_GOLD_*` variable are removed before the in-memory Gold environment is
added. The local semantic facade is enabled by default with the allowlisted
`Qwen/Qwen3-Embedding-0.6B` model, 1024 dimensions, `cpu` device, and local
files only. Model download remains disabled unless the operator explicitly adds
`--allow-embedding-download`; status output reports capability booleans without
printing model, device, URI, username, or password values. The launcher does not
start or restart Docker, stores no secret artifact,
and is a local development convenience only. It is not a production secret
manager, access-control layer, or deployment credential strategy.
