@echo off
setlocal

set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%src"
if "%NCS_DB_PATH%"=="" set "NCS_DB_PATH=%ROOT%data\processed\ncs.db"
if "%NCS_MCP_READ_ONLY%"=="" set "NCS_MCP_READ_ONLY=1"
set "PYTHONUTF8=1"
set "PYTHON_EXE=%ROOT%.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

cd /d "%ROOT%"
"%PYTHON_EXE%" -m ncs_mcp.server
