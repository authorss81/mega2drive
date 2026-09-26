# Install the repo's version-controlled git hooks for this clone.
#
# The hook lives in .githooks/ (tracked by git) instead of .git/hooks/ (not
# tracked), and core.hooksPath points git at it. Re-run this after cloning, or
# on a new machine.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Set-Location $root
git config core.hooksPath .githooks

if (-not (Test-Path ".githooks\pre-commit")) {
    Write-Error "pre-commit hook not found at .githooks\pre-commit"
}

Write-Host ""
Write-Host "Installed. core.hooksPath = $(git config core.hooksPath)"
Write-Host ""
Write-Host "The pre-commit hook now runs guard_secrets.py on every commit."
Write-Host "Test it by trying to commit a file containing a real-looking email."
Write-Host ""
Write-Host "To remove:  git config --unset core.hooksPath"
Write-Host ""
