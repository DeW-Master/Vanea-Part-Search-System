@echo off
REM ============================================================
REM  Install git pre-push hook (Windows)
REM  Copies part-search-system/scripts/pre-push to .git/hooks/pre-push
REM ============================================================

setlocal

for /f "delims=" %%i in ('git rev-parse --show-toplevel 2^>nul') do set "REPO_ROOT=%%i"
if not defined REPO_ROOT (
    echo [install-hooks] Not inside a git repository.
    exit /b 1
)

set "SRC=%~dp0pre-push"
set "DST=%REPO_ROOT%\.git\hooks\pre-push"

if not exist "%REPO_ROOT%\.git\hooks" (
    echo [install-hooks] .git\hooks directory not found.
    exit /b 1
)

copy /Y "%SRC%" "%DST%" >nul
if errorlevel 1 (
    echo [install-hooks] Failed to copy hook.
    exit /b 1
)

echo [install-hooks] Installed pre-push hook to %DST%
echo [install-hooks] README build status will auto-refresh before every git push.

endlocal
