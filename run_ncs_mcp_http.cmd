@echo off
setlocal

if "%NCS_MCP_HOST%"=="" set NCS_MCP_HOST=127.0.0.1
if "%NCS_MCP_PORT%"=="" set NCS_MCP_PORT=8766
if "%NCS_MCP_READ_ONLY%"=="" set NCS_MCP_READ_ONLY=1
set "REMOTE_BIND_ARG="
if "%NCS_MCP_ALLOW_REMOTE_BIND%"=="1" set "REMOTE_BIND_ARG=--allow-remote-bind"

set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"

set "PYTHONPATH=%REPO_ROOT%\src"
set "PYTHON_EXE=%REPO_ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"
cd /d "%REPO_ROOT%"
"%PYTHON_EXE%" -m ncs_mcp.server --transport streamable-http --host "%NCS_MCP_HOST%" --port "%NCS_MCP_PORT%" %REMOTE_BIND_ARG%
