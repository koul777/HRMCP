# Institutional Chatbot Operational Contract Example

Status: example template; all institution integration controls are unverified.
Repository support is implementation context, not proof that an institution has
deployed, tested, or approved a control.

## Runtime Boundary

The supported baseline uses a read-only prepared SQLite database. Its request
path is authenticated reference chat UI/API -> public NCS tool execution
through the private NCS MCP -> database. An
institution-approved LLM gateway and model adapter may be added in front of the
public tool layer, but it is optional. The operator path uses separate
identities, credentials, processes, and interfaces. Chat requests must never
start collection, preprocessing, review-apply, or other write workflows.

The gateway exposes only public MCP tools, preserves structured errors and route
fingerprints, and presents recommendations as planning guidance. It must not
convert missing evidence into facts or set `human_reviewed`, `accepted`, or
`reviewed` without an explicit authorized human decision through the operator
process.

## Required Controls

| Control ID | Operational requirement | Evidence needed before `implemented` and `tested` may be true |
| --- | --- | --- |
| `llm_gateway` | Optional: use an institution-approved, replaceable model adapter; pin model/config versions; enforce the public-tool allowlist, timeouts, bounded retries, and cost/rate limits outside prompt text. | When enabled, provide gateway configuration review plus tool-denial, timeout, retry, and model-version test evidence. |
| `identity_access` | Enforce SSO, least-privilege user groups, session expiry, service identity, and private network access. | IAM policy, group-membership test, denied-access test, and session-expiry test. |
| `private_mcp_hosting` | Place MCP behind the institution TLS/auth gateway, restrict network exposure, supervise the process, and monitor health/readiness. | Deployment manifest, network/TLS test, denied direct-access test, and health alert test. |
| `read_only_data_volume` | Set read-only server mode, mount a closed prepared DB read-only, disable operator tools, and keep refresh/collection jobs separate. | Runtime settings, mount inspection, blocked-write test, DB hash/sidecar check, and named refresh owner. |
| `audit_logging` | Record pseudonymous actor/request IDs, route fingerprint, tool, release/DB version, timing, outcome, and error code; include model adapter/version only when an LLM is enabled. Redact secrets, prompts, and personal data by default. | Log schema, redaction test, access review, retention rule, and sample event with non-sensitive values. |
| `operator_separation` | Use separate operator roles and credentials; keep review decisions, guarded collection, and apply actions unavailable to chatbot users. | Role matrix plus negative tool-discovery/execution tests and operator workflow evidence. |
| `security_privacy` | Classify chat and HR data, minimize prompt/log fields, define transcript opt-in, retention/deletion, processor/model boundaries, vulnerability handling, and privacy approval. | Approved policy references, data-flow review, deletion test, redaction test, and security/privacy sign-off. |
| `backup_restore_rollback` | Define RPO/RTO; encrypt and retain versioned DB/config/release backups; use sidecar-safe snapshots; test restore and source/DB rollback compatibility. | Backup job result, integrity hash, timed restore drill, rollback drill, and named rollback authority. |
| `capacity_incident_response` | Measure target-host concurrency, bound queues and retries, surface `service_busy`, alert on saturation/failures, and maintain shutdown, credential-rotation, notification, and post-incident procedures. | Load result and SLO, overload test, alert/on-call test, incident exercise, and current response contacts. |

## Evidence Rules

- Keep `implemented=false`, `tested=false`, and
  `verification_status=unverified` until institution-owned evidence exists.
- `repository_support_refs` may show backend support only. They do not satisfy an
  institution control and are not consumed as readiness evidence.
- Set `implemented=true` only with a named accountable owner and a deployed
  configuration artifact. Set `tested=true` only with a dated result from the
  target environment. Put those institution artifacts in `evidence_refs`.
- Evidence references must not contain credentials, secret values, raw personal
  data, or full transcripts. Access-restricted evidence may be referenced by an
  opaque record ID.
- Keep `report_only=true`, `status_update_allowed=false`, `db_writes=false`, and
  `approval_claim=false`. The integration report is readiness evidence, not an
  approval record or DB mutation instruction.

## Operating Decisions

Before a pilot, record the user group, service and data
owners, allowed public tools, data classification, transcript and audit
retention, SLO, capacity limit, RPO/RTO, backup schedule, rollback authority,
on-call route, and MCP shutdown authority. When an LLM is enabled, also record
the model adapter/version and model shutdown authority. Exercise denied access,
read-only enforcement, overload handling, restore, rollback, and incident
escalation against the exact target environment.

All eight required controls need a non-empty owner and evidence references
before the institution readiness report can mark private-pilot integration
ready. The optional `llm_gateway` control does not block that state.
