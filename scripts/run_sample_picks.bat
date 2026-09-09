@echo off
REM Wrapper for Windows Task Scheduler / manual double-click. Runs
REM sample_picks.py for the most recently finished gameweek (default
REM behaviour — omit --gw) with the project venv, appending output to a
REM gitignored log file.
setlocal
cd /d "%~dp0.."
if not exist "cache\logs" mkdir "cache\logs"
".venv\Scripts\python.exe" "scripts\sample_picks.py" -v >> "cache\logs\sample_picks.log" 2>&1
endlocal
