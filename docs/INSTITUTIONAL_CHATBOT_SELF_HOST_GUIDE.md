# Institutional NCS HR Chatbot Self-Host Guide

This repository is sufficient to provide the NCS reasoning and evidence layer
for an internally built HRD chatbot. An institution does not need to procure a
second recommendation engine or outsource the core NCS mapping logic. It still
needs to assemble and operate the user-facing service around this MCP server.

## What The Repository Already Provides

- NCS hierarchy, task, performance-criteria, and KSA evidence in SQLite.
- Training-course, delivery, career-path, qualification, and job-base links.
- Natural-language query routing with a stable public MCP tool contract.
- Read-only education-path, task-training, and transition recommendation tools.
- `recommended_path`, `training_system_matrix`, and guide-trace output.
- Evidence-oriented dashboard, release gates, review packets, and audit reports.
- STDIO and private HTTP transports, health/readiness endpoints, Docker metadata,
  and CI smoke checks.

These capabilities are the domain backend. The repository also includes a
read-only reference chat application in `ncs_mcp.institutional_chat`. It is a
usable route-first UI/API for the supported NCS workflows, but it does not
replace an institution identity provider, TLS gateway, approved language model,
or production security control plane.

## Reference Chat Application

The reference application provides:

- A responsive Korean chat screen at `/` and a JSON `POST /api/chat` endpoint.
- Natural-language routing through the same `ncs_query_route_v1` contract used
  by MCP discovery and the dashboard.
- Execution restricted to the public meta-executable NCS tool allowlist.
- Explicit blocking of operator, collection, review, and approval routes.
- Clarification responses when required route parameters are missing.
- Structured course/evidence summaries, disclaimers, and overload errors.
- Optional gateway authentication with user/group headers, Origin enforcement,
  and prompt-free pseudonymous JSONL audit events.
- Bounded HTTP worker threads and request-read timeouts; excess connections are
  held behind accept-stage backpressure instead of creating unbounded threads.
- Startup refusal unless MCP serving is read-only and operator tools are off.

Start a loopback-only local evaluation against a prepared database:

```powershell
$env:PYTHONPATH="C:\workspace\NCS_MCP\src"
$env:NCS_DB_PATH="C:\secure-data\ncs.db"
$env:NCS_MCP_READ_ONLY="1"
$env:NCS_MCP_ENABLE_OPERATOR_TOOLS="0"
.\run_ncs_institutional_chat.cmd
```

Open `http://127.0.0.1:8780`. Local mode is for a named analyst workstation or
development evaluation only; it is not SSO evidence.

For institution gateway mode, the reverse proxy must remove client-supplied
identity headers, authenticate the session, and inject
`X-NCS-Gateway-Secret`, `X-Authenticated-User`, and
`X-Authenticated-Groups`. Configure the backend with:

```powershell
$env:NCS_CHAT_HOST="0.0.0.0"
$env:NCS_CHAT_AUTH_MODE="gateway"
$env:NCS_CHAT_ALLOW_REMOTE_BIND="1"
$env:NCS_CHAT_GATEWAY_SECRET="<secret-from-institution-secret-store>"
$env:NCS_CHAT_ALLOWED_ORIGINS="https://hr-chat.example.internal"
$env:NCS_CHAT_ALLOWED_GROUPS="hrd-chat-users"
$env:NCS_CHAT_AUDIT_LOG_PATH="C:\secure-logs\ncs-chat-audit.jsonl"
$env:NCS_CHAT_AUDIT_HASH_SALT="<salt-from-institution-secret-store>"
$env:NCS_CHAT_MAX_HTTP_WORKERS="32"
$env:NCS_CHAT_REQUEST_SOCKET_TIMEOUT_SECONDS="15"
.\run_ncs_institutional_chat.cmd
```

For service managers and containers, use file-backed secrets instead of the two
direct secret variables:

```powershell
$env:NCS_CHAT_GATEWAY_SECRET_FILE="C:\secure-secrets\chat-gateway-secret.txt"
$env:NCS_CHAT_AUDIT_HASH_SALT_FILE="C:\secure-secrets\chat-audit-salt.txt"
```

Set only one source for each secret. Secret files must be small, non-empty,
single-value UTF-8 files. Startup fails when both the direct and file-backed
form are set.

Do not place either secret in source control, browser JavaScript, proxy access
logs, or evidence artifacts. The application rejects a non-loopback bind unless
the explicit remote flag, gateway authentication, allowed Origin, audit path,
and hash salt are all present. The audit schema records a keyed identity hash,
route fingerprint, public tool, release version, timing, outcome, and error
code; it does not record prompts or tool results. Authentication, Origin,
authorization, body-validation, timeout, and HTTP-capacity denials are logged
as rejection events without storing the rejected prompt body.

Recommendation-capacity saturation still returns retryable `service_busy` from
the public tool layer. HTTP-worker saturation uses backpressure so the server
does not reset a client while its request body is still being sent.

The built-in worker and socket bounds are defense in depth, not a replacement
for the institution reverse proxy. The proxy must also buffer request bodies,
enforce header/body/time limits, rate-limit users, and cap upstream concurrency.

### Hardened Compose Baseline

`deploy/compose.institutional-chat.yml` runs the reference chat with a read-only
root filesystem, all Linux capabilities dropped, a read-only prepared DB bind,
Docker secret mounts, and a dedicated writable audit volume. Host publication
is loopback-only and still requires an institution TLS/SSO reverse proxy.

Configure non-secret deployment values with
`deploy/institutional-chat.env.example`, point both secret-file variables at
host files managed outside the repository, and run:

```powershell
docker compose --env-file deploy\institutional-chat.env -f deploy\compose.institutional-chat.yml config
docker compose --env-file deploy\institutional-chat.env -f deploy\compose.institutional-chat.yml up -d --build
```

The `config` preflight must not be archived because its rendered output can
contain host secret-file paths. Secret contents remain mounted under
`/run/secrets` and are not placed in container environment variables.

## Components The Institution Must Assemble

```text
Employee or HR planner
  -> institution chat UI or reference route-first chat
  -> optional institution-approved LLM gateway/model adapter
  -> private NCS MCP HTTP service
  -> mounted read-only serving database
  -> NCS task/KSA/training evidence
```

Keep the operator path separate:

```text
HR/domain reviewer or data operator
  -> protected operator workstation or dashboard
  -> review packets and guarded collection plans
  -> explicit human decision or operator-timed job
```

The institution owns the following integration work:

| Component | Minimum responsibility |
| --- | --- |
| Chat UI | Deploy and institution-test the reference UI or replace it with an approved equivalent; add feedback ownership. |
| LLM gateway | Optional for generative conversation; own the model contract, system prompt, tool calling, rate limits, and cost controls. |
| Identity and access | SSO, user/group authorization, session expiry, and private network exposure. |
| Service hosting | Private reference chat service, TLS/reverse proxy, process supervision, and health checks. |
| Data volume | Prepared SQLite delivery, read-only serving mount, backup, restore, and refresh ownership. |
| Audit logging | User/request identifier, route fingerprint, tool name, timing, outcome, and release version. |
| Review operations | Named HR/domain reviewers, decision packets, rationale, timestamp, and guarded apply policy. |
| Security and privacy | Data classification, retention, log redaction, vulnerability response, and institutional approval. |

`docs/examples/institutional_chatbot_system_prompt.md` is a conservative
starting prompt for gateway review. Prompt text is not an access-control
mechanism; the gateway must enforce its public-tool allowlist independently.

## Recommended Runtime Boundary

- The reference chat executes only the 9 meta-executable public tools. A custom
  gateway may discover the default 11 public MCP tools but must keep the same
  read-only and operator-denial boundary.
- Keep `NCS_MCP_ENABLE_OPERATOR_TOOLS=0` in the serving process.
- Keep `NCS_MCP_READ_ONLY=1`; this opens SQLite with `mode=ro` and
  `PRAGMA query_only=ON`, and skips schema initialization on tool calls.
  Read-only mode also suppresses the operator MCP surface even if the operator
  environment flag is set accidentally.
- Start with `NCS_MCP_MAX_CONCURRENT_RECOMMENDATIONS=2` and
  `NCS_MCP_RECOMMENDATION_QUEUE_TIMEOUT_SECONDS=30` per process. A request that
  cannot obtain capacity returns retryable `service_busy`; the gateway should
  apply bounded retries and overload messaging.
- Bind directly to loopback or a private service network; put TLS and SSO at the
  institution reverse proxy or gateway. The server rejects non-loopback HTTP
  binds unless `--allow-remote-bind` is explicit. That opt-in only acknowledges
  the network boundary; it does not add authentication or TLS.
- Mount the prepared database separately from the source package. Normal chat
  serving is read-only and must not run collection, preprocessing, import, or
  review-status apply commands.
- Keep API keys in the collection/operator environment. Normal chatbot users do
  not need source API keys when using a prepared database.
- Store route and evidence identifiers needed for audit, but do not copy secret
  values or unnecessary personal data into model prompts or logs.

## Minimal Internal Pilot

1. Install the source preview in an internal Python 3.11+ environment.
2. Mount or hand off a prepared `ncs.db` and configure `NCS_DB_PATH`.
3. Run the MCP contract, lint, tests, STDIO smoke, and HTTP health smoke.
4. Start the loopback reference chat for route-first workflows, or start private
   HTTP MCP and register it in the institution-approved LLM gateway.
5. Render recommendation evidence and review warnings in the chat response.
6. Limit the pilot to named HRD users and collect structured feedback.

Baseline verification:

```powershell
python scripts\export_mcp_tool_contract.py --check --out mcp\ncs-tool-contract.json
python scripts\ncs_harness.py lint
python -m unittest discover -s tests -v
python scripts\mcp_stdio_smoke.py --timeout 20
python scripts\mcp_http_health_smoke.py --timeout 20
python scripts\prepare_serving_database.py --source-db <active-ncs.db> --output-db <new-serving-ncs.db> --out <snapshot-report.json> --markdown-out <snapshot-report.md> --quick-check
python scripts\benchmark_chatbot_readiness.py --db <prepared-ncs.db> --out <benchmark.json> --markdown-out <benchmark.md> --current-query "HR manager" --target-query "HR planning"
python scripts\institutional_chat_smoke.py --out <chat-smoke.json>
python scripts\institutional_chatbot_readiness_report.py --release-readiness <release.json> --deployment-decision <deployment.json> --chatbot-benchmark <benchmark.json> --source-preview-summary <preview-summary.json> --institutional-chat-smoke <chat-smoke.json> --out <institution-readiness.json> --markdown-out <institution-readiness.md>
```

The resulting report separates `core_backend_ready`,
`private_pilot_backend_ready`, `private_pilot_ready`, and
`stable_release_ready`. To assess the institution-owned layer, start from
`docs/examples/institutional_chatbot_integration_evidence.example.json`, keep
every control false until it has a named owner and test evidence, update its
timestamp, and pass it with `--institution-integration-evidence`. The checked-in
template is intentionally stale and incomplete; it cannot make a pilot ready.

Start the service only after the configured database is ready:

```powershell
$env:NCS_MCP_ENABLE_OPERATOR_TOOLS="0"
$env:NCS_MCP_READ_ONLY="1"
.\run_ncs_mcp_http.cmd
```

The default endpoints are `/mcp`, `/health`, and `/ready` on port `8766`.
For a containerized loopback-only pilot, set `NCS_DB_HOST_PATH` to the prepared
database file and use the hardened example:

```powershell
$env:NCS_DB_HOST_PATH="C:\secure-data\ncs.db"
docker compose -f deploy\compose.internal.yml up --build -d
```

The example mounts only the DB file as read-only, binds the host port to
`127.0.0.1`, disables operator tools, removes Linux capabilities, and makes the
container root filesystem read-only. Put the institution gateway in front of
that loopback endpoint; do not change the host bind to a public interface as a
substitute for SSO or TLS.

## Chatbot Response Contract

The chat orchestrator should preserve these product boundaries:

- Ask for missing job, task, target population, level, method, facility, or hour
  constraints when the route reports missing parameters.
- Display the NCS scope, task/KSA basis, course fit, delivery evidence, and human
  review state returned by the planner.
- Describe recommendations as education-planning guidance, not official
  qualification, legal eligibility, hiring, promotion, or mandatory-training
  approval.
- Keep required/optional course classification and annual-plan adoption pending
  until an authorized human reviewer decides.
- Do not use the 2026 HRD guide as source training data or as a score boost.
- Do not activate SQF or learning-module evidence in the normal NCS route.

## Internal Pilot Acceptance Criteria

An institution can call the chatbot pilot-ready when all of the following are
true:

- The exact source-preview tree passes hash, compile, secret, lint, unit, STDIO,
  HTTP health/readiness, and harness smoke checks.
- Dashboard verification is `ok=true` and live plans have no missing matrix,
  path, guide-trace, or query-route fields.
- The chatbot can complete representative education-system, transition, and
  task-training conversations against the prepared database.
- SSO/private-network access, operator separation, log redaction, backup, and
  rollback have named owners and test evidence.
- Operator tools are disabled for chatbot users, and collection/API jobs are not
  launched by chat requests.
- Known review and data-coverage limitations are shown to pilot users.

Stable internal release additionally requires the active release-readiness
artifact to report `release_ready=true`. A technically healthy private pilot is
not permission to bypass human-review, provenance, or qualification-coverage
gates.

## Operations Decisions Before Stable Service

Record these values in the institution service plan instead of leaving them as
implicit platform assumptions:

| Decision | Required evidence |
| --- | --- |
| Availability and latency | Service window plus measured p50/p95 latency for representative planning routes and the agreed threshold. |
| RPO | Maximum acceptable loss window for the prepared DB, review decisions, and configuration. |
| RTO | Restore target plus a timed restore exercise from a known backup. |
| Backup | Owner, schedule, encryption, retention, integrity check, and sidecar-safe copy procedure. |
| Rollback | Previous source image and DB snapshot, compatibility check, and named rollback authority. |
| Chat retention | Whether transcripts are stored, retention period, deletion path, and fields excluded or redacted. |
| Audit retention | Route fingerprint, tool, release version, outcome, reviewer decision provenance, and access policy. |
| Capacity | Expected concurrent users, request rate, model/tool timeout, queue limit, and overload behavior. |
| Incident response | On-call owner, secret rotation, model/MCP shutdown, user notice, and post-incident evidence review. |

Run at least one restore-and-rollback exercise before expanding beyond a named
pilot group. The serving database, source release, and review evidence must be
versioned together closely enough to reconstruct which evidence a user saw.

The current 12.6 GB prepared database passed the sequential report-only
benchmark with all 12 measured workflow runs valid, p50 `1616.889 ms`, p95
`2046.962 ms`, and an unchanged full-file SHA-256 before and after. At
concurrency 2, all 8 measured runs remained valid with p95 `3548.887 ms` and
throughput `0.905 req/s`. An unbounded concurrency-4 run saturated the local
host: p95 rose to `22671.904 ms` and throughput fell to `0.31 req/s` even
though result validity and DB immutability remained intact. Treat these as
local reference measurements, not universal production SLOs. With the
per-process recommendation guard fixed at 2, the same concurrency-4 offered
load against a closed `DELETE`-journal serving snapshot completed with p95
`7192.087 ms`, throughput `0.854 req/s`, and queue-wait p95 `3690.4 ms`. All 12
results were valid; the main file and all WAL/SHM/journal sidecar states were
unchanged, with no filesystem mutation observed. A benchmark against the active
WAL database correctly failed strict immutability because SQLite touched SHM
lock metadata even though the main and WAL content hashes stayed unchanged.
Prepare and benchmark the exact closed serving snapshot, keep the guard at 2
initially, and use reported queue-wait latency to decide when a gateway queue or
measured multi-process/read-replica deployment is needed.

## Procurement Boundary

Core NCS application development can remain in-house with this repository.
External procurement may still be needed for services rather than domain code:

- LLM/API usage or an internally hosted model platform.
- Server, container, monitoring, backup, and security tooling.
- Identity-provider or enterprise integration licenses.
- Independent security, privacy, accessibility, or compliance assessment when
  institutional policy requires it.

Those purchases do not require replacing the NCS MCP, ontology, recommendation,
or evidence workflow implemented here.

## Current Release Interpretation

Use `private/draft developer preview` while engineering verification passes but
the release report still lists human-review or data-coverage blockers. Use
`stable internal release` only after those gates close. Public anonymous hosting
and multi-tenant service remain outside the active product scope.

See also:

- `docs/AIHR_DEPLOYMENT_RUNBOOK.md`
- `docs/AIHR_PRODUCTIZATION_STRATEGY.md`
- `docs/MCP_RELEASE_CHECKLIST.md`
- `docs/NCS_MCP_PRD.md`
