@echo off
REM Wrapper for Windows Task Scheduler. Runs snapshot_odds.py from the
REM repo root with the project venv, appending output to a gitignored log
REM file (cache\ is already excluded — see .gitignore). Mirrors
REM run_snapshot_bootstrap.bat exactly (session s005) — this script is now
REM meant to be fired on a frequent, dumb interval; snapshot_odds.py itself
REM decides on every firing whether this is one of the four deadline-
REM relative capture points (see that script's module docstring). No -v
REM here, same as bootstrap's wrapper: the urllib3 credential-leak fix
REM (scripts/snapshot_odds.py's SECURITY comment) does not depend on that,
REM but plain -v is deliberately never added to a scheduled invocation.
setlocal
cd /d "%~dp0.."
if not exist "cache\logs" mkdir "cache\logs"
".venv\Scripts\python.exe" "scripts\snapshot_odds.py" >> "cache\logs\snapshot_odds.log" 2>&1
endlocal
