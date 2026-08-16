@echo off
setlocal

if "%NCS_MCP_HOST%"=="" set NCS_MCP_HOST=127.0.0.1
if "%NCS_MCP_PORT%"=="" set NCS_MCP_PORT=8766

set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"

set "PYTHONPATH=%REPO_ROOT%\src"
cd /d "%REPO_ROOT%"
python -m ncs_mcp.server --transport streamable-http --host "%NCS_MCP_HOST%" --port "%NCS_MCP_PORT%"
