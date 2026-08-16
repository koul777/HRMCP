# MCP Error Codes

NCS MCP tool failures use the common envelope:

```json
{
  "ok": false,
  "data": {},
  "error": {
    "code": "invalid_tool_parameters",
    "category": "validation",
    "retryable": false,
    "known": true
  },
  "audit": {}
}
```

## Categories

- `not_found`: Requested NCS, ontology, training, or legacy reference record was
  not found. Do not hallucinate a replacement.
- `validation`: Caller input is missing, malformed, too broad, or too ambiguous.
- `unsupported`: The requested mode, status, type, or trust policy is not allowed.
- `policy`: MCP meta-execution policy blocked the call.
- `execution`: Tool handler failed after arguments were accepted.
- `configuration`: Required local configuration or API key is missing.
- `external_dependency`: External API call failed and may be retried.
- `application`: Fallback category for uncatalogued errors.

## Common Codes

| Code | Category | Retryable | Meaning |
| --- | --- | --- | --- |
| `NOT_FOUND` | `not_found` | false | Generic not-found envelope. |
| `concept_not_found` | `not_found` | false | Ontology concept id was not found. |
| `ncs_unit_not_found` | `not_found` | false | NCS competency unit was not found. |
| `missing_task_locator` | `validation` | false | A criteria id, unit code, or query is required. |
| `missing_transition_query` | `validation` | false | Current/target transition query is required. |
| `low_quality_query` | `validation` | false | Query is too short or ambiguous for ranking. |
| `invalid_tool_parameters` | `validation` | false | MCP tool arguments do not match the tool signature. |
| `unsupported_analysis_mode` | `unsupported` | false | `ncs_analysis.mode` is not one of the allowed modes. |
| `meta_tool_recursion_blocked` | `policy` | false | `ncs_execute_tool` cannot execute itself or discovery. |
| `tool_not_executable_via_meta` | `policy` | false | Operator/review/legacy tool is blocked from meta execution. |
| `tool_execution_failed` | `execution` | false | Handler failed after argument binding succeeded. |
| `qualification_service_key_missing` | `configuration` | false | Qualification collection key is absent. |
| `job_base_service_key_missing` | `configuration` | false | Job-base collection key is absent. |
| `external_api_error` | `external_dependency` | true | External API failed; retry may be appropriate. |

## Not-Found Contract

Specific not-found codes remain in `error.code`, for example
`concept_not_found`. The response may also include the `[NOT_FOUND]` text marker
inside `content` for LLM guidance. Client code should branch on `error.code` or
`error.category`, not on the text marker.

## Secret Handling

Error payloads are masked before leaving the server boundary. URL query
parameters such as `authKey`, `serviceKey`, `apiKey`, and known NCS service-key
environment values are replaced with `[REDACTED]`.
