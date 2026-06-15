# NCS-SQF MCP Experiment Guide

This repository exposes the NCS-SQF education recommendation system as a stdio
MCP server.

## Files

- `mcp/ncs-mcp.json`: MCP host configuration snippet.
- `run_mcp_server.bat`: Windows batch launcher.
- `scripts/run_mcp_server.ps1`: PowerShell launcher with a `-Check` mode.
- `src/ncs_mcp/server.py`: actual FastMCP stdio server.

## Check The Local Server Path

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_mcp_server.ps1 -Check
```

Expected output is the SQLite DB path:

```text
C:\Workplace\NCS_MCP\data\processed\ncs.db
```

## MCP Host Config

Use the contents of `mcp/ncs-mcp.json` in an MCP host that accepts an
`mcpServers` config block, such as Claude Desktop or another local MCP client.

```json
{
  "mcpServers": {
    "ncs-sqf-education": {
      "command": "python",
      "args": ["-m", "ncs_mcp.server"],
      "env": {
        "PYTHONPATH": "C:/Workplace/NCS_MCP/src",
        "NCS_DB_PATH": "C:/Workplace/NCS_MCP/data/processed/ncs.db",
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

The server is a stdio process. It is normally started by the MCP host, not by
double-clicking it. If you run it manually, it waits for MCP JSON-RPC messages on
stdin.

## Useful First Tool Calls

```json
{"tool": "search_sqf_jobs", "arguments": {"keyword": "HR", "major_code": "02", "limit": 5}}
```

```json
{"tool": "recommend_education_for_duty", "arguments": {"query": "HR", "major_code": "02", "limit": 3}}
```

```json
{"tool": "search_learning_modules", "arguments": {"query": "HR", "major_code": "02", "limit": 5}}
```

```json
{"tool": "search_ontology_concepts", "arguments": {"query": "workforce", "concept_type": "knowledge", "limit": 5}}
```

After a saved recommendation is created, use:

```json
{"tool": "explain_education_recommendation", "arguments": {"recommendation_run_id": 1, "rank": 1}}
```

## Notes

- API keys are not required for read-only MCP experiments against the generated
  SQLite DB.
- Recommendation results are evidence-based guidance, not official SQF
  recognition, qualification, or legal eligibility decisions.
- Trusted education recommendations use accepted/reviewed/human-reviewed
  SQF-NCS mappings only.
