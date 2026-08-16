# Legacy SQF SQLite Notice

SQF SQLite ontology workflows are deprecated for the active MCP.

Current implementation work should use:

- `src/ncs_mcp/training_course_api.py`
- `src/ncs_mcp/training_recommendation.py`
- KSA/task ontology functions in `src/ncs_mcp/db.py`
- MCP tools in `src/ncs_mcp/server.py`

SQF tables can remain in existing SQLite databases for compatibility but should not be used by active recommendations.
