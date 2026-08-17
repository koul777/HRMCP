@echo off
setlocal

set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"

set "DASHBOARD_LAUNCHER=%REPO_ROOT%\scripts\run_dashboard.ps1"
if not exist "%DASHBOARD_LAUNCHER%" (
    echo [NCS Knowledge Graph] Launcher not found:
    echo %DASHBOARD_LAUNCHER%
    pause
    exit /b 1
)

echo Starting NCS Knowledge Graph...
powershell -NoProfile -ExecutionPolicy Bypass -File "%DASHBOARD_LAUNCHER%" -Port 8767 -OpenPath "/ncs-knowledge-graph" -Restart %*

if errorlevel 1 (
    echo.
    echo Failed to start NCS Knowledge Graph.
    echo Check logs\dashboard.out.log and logs\dashboard.err.log.
    pause
    exit /b 1
)

endlocal
