# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
# Stage a private Windows RC installer using the owner's locally held release key.
# The result is NOT an official release until version/tag/provenance/publication checks pass.
[CmdletBinding()]
param(
    [string]$KeyPath = (Join-Path $HOME 'NatureAI-Release-Keys\release-key.pem'),
    [string]$StageDir = '',
    [string]$ISCC = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $StageDir) { $StageDir = Join-Path $repo 'dist\windows-rc-stage-0.1.1' }
$expectedKeyId = '64ecfc2da2ed12e9'
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Dedicated .venv Python not found; create it with py -3.13 -m venv .venv first.'
}

if ($env:OS -ne 'Windows_NT') { throw 'This helper must run on Windows.' }
if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
    throw "Signing key not found: $KeyPath"
}
if (Test-Path -LiteralPath $StageDir) {
    throw "Output directory already exists; refusing to overwrite: $StageDir"
}
$branch = (& git -C $repo branch --show-current).Trim()
if ($LASTEXITCODE -ne 0 -or $branch -ne 'release/v0.1.0-rc-windows-first') {
    throw "Check out release/v0.1.0-rc-windows-first first (current: $branch)."
}
$dirty = & git -C $repo status --porcelain
if ($LASTEXITCODE -ne 0 -or $dirty) {
    throw 'Working tree is not clean. Commit or stash unrelated changes first.'
}
$commit = (& git -C $repo rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve Git HEAD.' }

# Do not put the passphrase on the command line, in PowerShell history or in the repository.
# The existing release_bundle.py reads it from this process's temporary environment.
$secure = Read-Host 'Official release key passphrase (input hidden)' -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $env:NATURE_RELEASE_KEY_PASSPHRASE = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    $secure.Dispose()
}

try {
    & $python (Join-Path $repo 'scripts\check_release_signing_key.py') $KeyPath $expectedKeyId
    if ($LASTEXITCODE -ne 0) { throw 'Signing-key verification failed; nothing was staged.' }

    Write-Host "Staging private RC from commit $commit"
    & $python (Join-Path $repo 'scripts\windows_installer.py') stage --private-key $KeyPath --notes (Join-Path $repo 'docs\RELEASE_NOTES_v0.1.1-rc.md') --min-upgrade-from 0.1.0 --out $StageDir
    if ($LASTEXITCODE -ne 0) { throw 'Windows installer staging failed.' }
}
finally {
    Remove-Item Env:NATURE_RELEASE_KEY_PASSPHRASE -ErrorAction SilentlyContinue
}

$info = Get-Content -LiteralPath (Join-Path $StageDir 'build-info.json') -Raw | ConvertFrom-Json
if ($info.min_upgrade_from -ne '0.1.0') {
    throw 'Release upgrade eligibility mismatch: expected 0.1.0 as minimum.'
}
if ($info.commit -ne $commit -or $info.trust_key) {
    throw 'Unexpected build provenance or local trust-key override; stop and inspect this private RC.'
}
Write-Host "Staged version: $($info.version), commit: $($info.commit)"
Write-Host 'Status: PRIVATE RC ONLY. This build is not an official tagged GitHub Release.'

if (-not $ISCC) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            $ISCC = $candidate
            break
        }
    }
}
if (-not $ISCC) {
    Write-Host 'Staging succeeded. Inno Setup 6 ISCC.exe was not found.'
    Write-Host "Compile later: ISCC.exe `"$StageDir\NatureAIWorker.iss`""
    exit 0
}
if (-not (Test-Path -LiteralPath $ISCC -PathType Leaf)) {
    throw "ISCC.exe not found: $ISCC"
}
& $ISCC (Join-Path $StageDir 'NatureAIWorker.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup compilation failed.' }
Get-ChildItem -LiteralPath (Join-Path $StageDir 'output') -Filter '*.exe' -File |
    ForEach-Object {
        $digest = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
        Write-Host "Installer: $($_.FullName)"
        Write-Host "SHA256: $digest"
    }
Write-Host 'Do not publish this installer as an official release without version/tag, acceptance and legal checks.'
