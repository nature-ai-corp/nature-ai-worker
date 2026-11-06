# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
# Read-only comparison; no Platform source is written into the Worker checkout.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [string]$PlatformRepo,
    [string]$PlatformRef = "HEAD"
)
$ErrorActionPreference = "Stop"
$workerRepo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $workerRepo ".venv\Scripts\python.exe"
$contract = Join-Path $workerRepo "src\nature_worker\contracts\client-interface-v1.openapi.json"
$compare = Join-Path $workerRepo "scripts\client_interface_contract.py"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Validated local Worker venv is required." }
if (-not (Test-Path -LiteralPath $contract -PathType Leaf)) { throw "Public Worker interface contract is missing." }
if (-not (Test-Path -LiteralPath $PlatformRepo -PathType Container)) { throw "PlatformRepo path unavailable." }
$commit = (& git -C $PlatformRepo rev-parse --verify "$PlatformRef^{commit}" 2>$null)
if ($LASTEXITCODE -ne 0) { throw "Platform Git reference not found." }
$commit = "$commit".Trim()
$temp = Join-Path ([System.IO.Path]::GetTempPath()) ("nature-worker-contract-compare-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temp -ErrorAction Stop | Out-Null
try {
    $privateJson = Join-Path $temp "platform-contract.json"
    $resultJson = Join-Path $temp "result.json"
    $errorFile = Join-Path $temp "stderr.txt"
    # Retrieve exactly the committed contract blob, never the live deployment,
    # credentials, workspace or entire Platform repository.
    $spec = $commit + ":docs/client-interface-v1.openapi.json"
    $gitResult = & git -C $PlatformRepo show $spec 2>$null
    if ($LASTEXITCODE -ne 0) { throw "Committed Platform contract not found at docs/client-interface-v1.openapi.json." }
    [System.IO.File]::WriteAllText($privateJson, ($gitResult -join "`n") + "`n", [System.Text.UTF8Encoding]::new($false))
    $argsQuoted = @("-I", $compare, "compare", "--ignore-descriptions", $privateJson, $contract) |
        ForEach-Object { '"' + $_.Replace('"', '\\"') + '"' }
    $p = Start-Process -FilePath $python -ArgumentList $argsQuoted -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $resultJson -RedirectStandardError $errorFile
    Write-Host "PLATFORM COMMIT : $commit"
    Write-Host "WORKER PUBLIC CONTRACT : local checked-out file"
    if (Test-Path -LiteralPath $resultJson) {
        try {
            $r = Get-Content -LiteralPath $resultJson -Raw | ConvertFrom-Json
            Write-Host "CONTRACT IDENTICAL : $($r.identical)"
            Write-Host "OPERATION COUNTS : $($r.operations -join ' / ')"
            Write-Host "SCHEMAS DIFFERING COUNT : $(@($r.schemas_differing).Count)"
            Write-Host "SECURITY SCHEMES EQUAL : $($r.security_schemes_equal)"
            Write-Host "OPERATIONS ONLY IN PLATFORM COUNT : $(@($r.only_in_a).Count)"
            Write-Host "OPERATIONS ONLY IN WORKER COUNT : $(@($r.only_in_b).Count)"
        } catch {
            Write-Host "CONTRACT COMPARE : ERROR (invalid comparison output)"
        }
    }
    if ($p.ExitCode -ne 0) { throw "CONTRACT COMPARE : FAIL / DIFFERENCE (review privately; no paths or schemas disclosed)" }
    Write-Host "CONTRACT COMPARE : PASS"
}
finally {
    Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
}
