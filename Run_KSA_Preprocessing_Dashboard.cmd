@echo off
setlocal

set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%REPO_ROOT%\scripts\run_dashboard.ps1" -Port 8766 -OpenPath /ksa-preprocessing-dashboard -Restart %*
