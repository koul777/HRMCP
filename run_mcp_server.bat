@echo off
setlocal

set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%src"
set "NCS_DB_PATH=%ROOT%data\processed\ncs.db"
set "PYTHONUTF8=1"

cd /d "%ROOT%"
python -m ncs_mcp.server
