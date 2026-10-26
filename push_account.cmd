@echo off
rem ===========================================================================
rem  Push the local config.yaml to GitHub Actions and trigger a pipeline run.
rem
rem  Runs from ANY folder. No PowerShell, so it is not affected by the
rem  execution policy that blocks .ps1 files on this machine.
rem
rem  Usage:
rem    push_account.cmd                 normal run (discover new accounts + upload)
rem    push_account.cmd list-only       inventory only, no transfers
rem    push_account.cmd retry-failed    re-attempt rows previously marked failed
rem    push_account.cmd run-all watch   run, then wait and print the result
rem
rem  Needs: gh CLI logged in once (gh auth login).
rem  Reads: %USERPROFILE%\.secrets\mega2drive\config.yaml
rem ===========================================================================
setlocal EnableExtensions

set "REPO=authorss81/mega2drive"
set "WF=mega-to-youtube-workflow.yml"
set "CONFIG=%USERPROFILE%\.secrets\mega2drive\config.yaml"
set "MODE=%~1"
set "WATCH=%~2"
if "%MODE%"=="" set "MODE=run-all"

set "VALID="
if /i "%MODE%"=="run-all"      set "VALID=1"
if /i "%MODE%"=="list-only"    set "VALID=1"
if /i "%MODE%"=="retry-failed" set "VALID=1"
if not defined VALID (
  echo ERROR: unknown mode "%MODE%"
  echo        use one of: run-all ^| list-only ^| retry-failed
  exit /b 1
)

if not exist "%CONFIG%" (
  echo ERROR: config not found at
  echo        %CONFIG%
  exit /b 1
)

echo ==^> Pushing CONFIG_YAML secret to %REPO%
rem Redirection, not `type | gh`, so the file is sent byte-for-byte.
gh secret set CONFIG_YAML --repo "%REPO%" < "%CONFIG%"
if errorlevel 1 goto fail
echo     ok

echo ==^> Triggering %WF% (mode=%MODE%)
gh workflow run "%WF%" --repo "%REPO%" --ref main -f mode=%MODE%
if errorlevel 1 goto fail

echo ==^> Waiting for the run to register...
set "RUNID="
for /l %%n in (1,1,20) do (
  if not defined RUNID (
    ping -n 4 127.0.0.1 >nul 2>&1
    for /f "usebackq delims=" %%i in (`gh run list --repo "%REPO%" --workflow "%WF%" --limit 1 --json databaseId --jq ".[0].databaseId"`) do set "RUNID=%%i"
  )
)

if not defined RUNID (
  echo     could not read the new run id - check https://github.com/%REPO%/actions
  exit /b 1
)

echo.
echo     run %RUNID%: https://github.com/%REPO%/actions/runs/%RUNID%
echo.

if /i not "%WATCH%"=="watch" (
  echo     Not waiting. To follow it:  gh run watch %RUNID% --repo %REPO%
  exit /b 0
)

gh run watch "%RUNID%" --repo "%REPO%" --exit-status >nul 2>&1
echo ==^> Result
rem Bare --jq expression on purpose: cmd.exe strips nested double quotes, and an
rem expression like ".status + \" / \" + .conclusion" then arrives as 3 arguments
rem ("accepts at most 1 arg(s), received 3"). A single field has no such problem.
gh run view "%RUNID%" --repo "%REPO%" --json conclusion --jq .conclusion
exit /b 0

:fail
echo.
echo FAILED - see the message above.
exit /b 1
