@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "scripts\run_ncs_builder.py"
) else (
  python "scripts\run_ncs_builder.py"
  if errorlevel 1 pause
)
