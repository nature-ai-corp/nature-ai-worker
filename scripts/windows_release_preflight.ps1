# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
# Run on the controlled release checkout; audit output stays local and private.
[CmdletBinding()]
param(
    [string]$CommittedPlatformContract = ""
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Missing local venv Python. Run from the validated Windows release checkout." }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$reportDir = Join-Path $repo "dist\private-release-preflight-$stamp"
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
$summary = Join-Path $reportDir "summary.txt"
function Write-Status([string]$name, [string]$status) {
    $line = "$name : $status"
    Add-Content -LiteralPath $summary -Value $line
    Write-Host $line
}
Set-Content -LiteralPath $summary -Value "NATURE AI Worker v0.1.1 private release preflight"
$commit = (& git -C $repo rev-parse HEAD).Trim()
Write-Status "COMMIT" $commit
$branch = (& git -C $repo branch --show-current).Trim()
Write-Status "BRANCH" $branch
$dirty = @(& git -C $repo status --porcelain)
if ($dirty.Count -gt 0) { Write-Status "WORKTREE" "FAIL (uncommitted changes)"; throw "Stop: repository is not clean." }
Write-Status "WORKTREE" "PASS"
$shallow = (& git -C $repo rev-parse --is-shallow-repository).Trim()
if ($shallow -eq "true") {
    Write-Status "GIT HISTORY" "NOT VERIFIED (shallow clone)"
    throw "Full-history scan requires a non-shallow clone."
}
& $python -m pytest -q (Join-Path $repo "tests\test_repo_history_scanning.py") *> (Join-Path $reportDir "regression.txt")
if ($LASTEXITCODE -ne 0) {
    Write-Status "REGRESSION" "FAIL (see private report)"
} else {
    Write-Status "REGRESSION" "PASS"
}
& $python (Join-Path $repo "scripts\repo_checks.py") --history *> (Join-Path $reportDir "history.txt")
if ($LASTEXITCODE -ne 0) {
    Write-Status "HISTORY AUDIT" "FAIL / REVIEW NEEDED (see private report)"
} else {
    Write-Status "HISTORY AUDIT" "PASS for all refs in this local clone"
}
if ($CommittedPlatformContract -ne "") {
    if (-not (Test-Path -LiteralPath $CommittedPlatformContract -PathType Leaf)) { throw "Committed Platform contract path is unavailable." }
    & $python -I (Join-Path $repo "scripts\client_interface_contract.py") compare --ignore-descriptions $CommittedPlatformContract (Join-Path $repo "src\nature_worker\contracts\client-interface-v1.openapi.json") *> (Join-Path $reportDir "contract.txt")
    if ($LASTEXITCODE -eq 0) {
        Write-Status "CONTRACT COMPARE" "PASS (authoritative committed Platform contract)"
    } else {
        Write-Status "CONTRACT COMPARE" "FAIL / DIFFERENCE (private report only)"
    }
} else {
    Write-Status "CONTRACT COMPARE" "NOT EXECUTED (authoritative committed Platform contract not supplied)"
}
Write-Host "Private report folder: $reportDir"
Write-Host "Share only summary.txt; never share raw audit reports without checking them for sensitive information."
