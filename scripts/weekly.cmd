@echo off
rem ===========================================================================
rem  Weekly batch entry point for Windows Task Scheduler.
rem  (This file is intentionally ASCII-only: cmd.exe misparses a UTF-8 BOM
rem   and mangles non-ASCII text depending on the console code page, which
rem   would break the script when the scheduler runs it non-interactively.
rem   Korean documentation for this file lives in README.md.)
rem
rem  Runs: detect -> compare -> LLM summary -> HWPX report -> DB load
rem  Output is appended to out\logs\weekly_<yyyyMMdd>.log
rem  Exit code is passed through so the scheduler can flag failures.
rem ===========================================================================
setlocal enabledelayedexpansion

rem UTF-8 console so Korean log output is not mangled.
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem Project root is the parent of this script's directory.
cd /d "%~dp0.."
if errorlevel 1 (
    echo Cannot enter project root: %~dp0..
    exit /b 1
)

if not exist "out\logs" mkdir "out\logs"

rem Date stamp in yyyyMMdd, locale-independent.
for /f "usebackq delims=" %%d in (`powershell -NoProfile -Command "(Get-Date).ToString('yyyyMMdd')"`) do set "STAMP=%%d"
if "%STAMP%"=="" set "STAMP=unknown"
set "LOG=out\logs\weekly_%STAMP%.log"

rem Prefer a project-local virtualenv, fall back to PATH python.
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

rem No space before >> : cmd would write that space into the log.
echo.>>"%LOG%"
echo ========================================================================>>"%LOG%"
echo [%DATE% %TIME%] weekly batch start ^(python: !PY!^)>>"%LOG%"
echo ========================================================================>>"%LOG%"

"!PY!" scripts\run_weekly.py --full >>"%LOG%" 2>&1
set "CODE=!ERRORLEVEL!"

echo [%DATE% %TIME%] weekly batch finished, exit code !CODE!>>"%LOG%"

rem Non-zero exit makes the task show as failed in Task Scheduler history,
rem which is what an operator should be alerted on.
exit /b !CODE!
