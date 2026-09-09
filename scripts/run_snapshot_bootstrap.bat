@echo off
REM Wrapper for Windows Task Scheduler. Runs snapshot_bootstrap.py from the
REM repo root with the project venv, appending output to a gitignored log
REM file (cache\ is already excluded — see .gitignore).
setlocal
cd /d "%~dp0.."
if not exist "cache\logs" mkdir "cache\logs"
".venv\Scripts\python.exe" "scripts\snapshot_bootstrap.py" >> "cache\logs\snapshot_bootstrap.log" 2>&1
endlocal
