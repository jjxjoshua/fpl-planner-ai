@echo off
REM Wrapper for Windows Task Scheduler. Runs snapshot_gameweek_stats.py from
REM the repo root with the project venv, appending output to a gitignored
REM log file (cache\ is already excluded -- see .gitignore). Mirrors
REM run_snapshot_bootstrap.bat/run_snapshot_odds.bat exactly (session s007,
REM story S0b): a dumb, argument-free, frequent-interval firing -- the ONE
REM fixed flag below (--sweep) selects the script's own "decide what to do
REM on every firing" mode (see snapshot_gameweek_stats.py's module
REM docstring, "Sweep mode"), it is not a per-run configuration value the
REM way --gw/--season are, so this wrapper still takes no arguments of its
REM own and never varies what it passes. No -v here, same as the other two
REM wrappers.
setlocal
cd /d "%~dp0.."
if not exist "cache\logs" mkdir "cache\logs"
".venv\Scripts\python.exe" "scripts\snapshot_gameweek_stats.py" --sweep >> "cache\logs\snapshot_gameweek_stats.log" 2>&1
endlocal
