# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
# Run on the controlled release checkout; audit output stays local and private.
[CmdletBinding()]
param(
    [string]$CommittedPlatformContract = ""
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "windows_release_common.ps1")
$sourceVersion = Get-SourceVersions $repo
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
function Invoke-PrivatePython([string[]]$Arguments, [string]$Name) {
    # PowerShell 5.1 turns native stderr with redirection into NativeCommandError
    # when ErrorActionPreference is Stop. Capture both streams as files.
    $stdout = Join-Path $reportDir ($Name + "-stdout.txt")
    $stderr = Join-Path $reportDir ($Name + "-stderr.txt")
    $argsQuoted = @($Arguments | ForEach-Object {
        '"' + $_.Replace('"', '\\"') + '"'
    })
    $process = Start-Process -FilePath $python -ArgumentList $argsQuoted -Wait -PassThru -NoNewWindow `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    return $process.ExitCode
}
# The version comes from the source being checked, never from this script.
Set-Content -LiteralPath $summary -Value "NATURE AI Worker private release preflight"
if ($sourceVersion.package -ne $sourceVersion.project) {
    Write-Status "VERSION" "FAIL (__init__.py $($sourceVersion.package), pyproject.toml $($sourceVersion.project))"
    throw "Stop: package and project versions differ."
}
Write-Status "VERSION" $sourceVersion.package
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
$regressionExit = Invoke-PrivatePython @("-m", "pytest", "-q", (Join-Path $repo "tests\test_repo_history_scanning.py")) "regression"
if ($regressionExit -ne 0) {
    Write-Status "REGRESSION" "FAIL (see private report)"
} else {
    Write-Status "REGRESSION" "PASS"
}
$historyExit = Invoke-PrivatePython @((Join-Path $repo "scripts\repo_checks.py"), "--history") "history"
if ($historyExit -ne 0) {
    Write-Status "HISTORY AUDIT" "FAIL / REVIEW NEEDED (see private report)"
    # Show only finding categories, not historical paths, hosts, IDs or values.
    $publicCategories = @(
        Get-Content -LiteralPath (Join-Path $reportDir "history-stdout.txt") |
            Where-Object { $_ -match '^(history-[a-z-]+|internal-address|confidential|secret|sensitive-file|release-artifact):' } |
            ForEach-Object { ($_ -split ':', 2)[0] } |
            Sort-Object -Unique
    )
    if ($publicCategories.Count -gt 0) {
        Write-Status "FINDING CATEGORIES" ($publicCategories -join ", ")
    }
} else {
    Write-Status "HISTORY AUDIT" "PASS for all refs in this local clone"
}
if ($CommittedPlatformContract -ne "") {
    if (-not (Test-Path -LiteralPath $CommittedPlatformContract -PathType Leaf)) { throw "Committed Platform contract path is unavailable." }
    $contractExit = Invoke-PrivatePython @("-I", (Join-Path $repo "scripts\client_interface_contract.py"), "compare", "--ignore-descriptions", $CommittedPlatformContract, (Join-Path $repo "src\nature_worker\contracts\client-interface-v1.openapi.json")) "contract"
    if ($contractExit -eq 0) {
        Write-Status "CONTRACT COMPARE" "PASS (authoritative committed Platform contract)"
    } else {
        Write-Status "CONTRACT COMPARE" "FAIL / DIFFERENCE (private report only)"
    }
} else {
    Write-Status "CONTRACT COMPARE" "NOT EXECUTED (authoritative committed Platform contract not supplied)"
}
Write-Host "Private report folder: $reportDir"
Write-Host "Share only summary.txt; never share raw audit reports without checking them for sensitive information."
