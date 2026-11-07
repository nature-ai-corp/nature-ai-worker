# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
# Build an official-release CANDIDATE from the owner's immutable, exact vX.Y.Z Git tag.
# No GitHub upload, publishing, tagging or repo visibility changes are performed.
#
#   git checkout --detach vX.Y.Z
#   powershell -File scripts\windows_official_release_stage.ps1 -Version X.Y.Z -KeyPath <key file>
#
# The version is never assumed: the tag, the source version, the notes file, the staged build
# and the signed manifest must all agree with -Version, and release_bundle.py verify must
# report a fully verified official release before the installer is compiled.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$MinUpgradeFrom = '0.1.0',
    [string]$KeyPath = '',  # required: no default, so no run ever picks up a key implicitly
    [string]$StageDir = '',
    [string]$ISCC = '',
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# First, before any other check or file access: the signing key is always named explicitly.
if (-not $KeyPath) { throw '-KeyPath is required: name the signing key file explicitly (there is no default key path).' }
. (Join-Path $PSScriptRoot 'windows_release_common.ps1')
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

Assert-ReleaseVersion $Version
Assert-ReleaseVersion $MinUpgradeFrom
$tag = "v$Version"
if ($env:OS -ne 'Windows_NT') { throw 'This helper must run on Windows.' }
if (-not $Python) { $Python = Join-Path $repo '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python not found: $Python (create the release venv with py -3.13 -m venv .venv)."
}
if (-not $StageDir) { $StageDir = Join-Path $repo "dist\windows-tagged-official-$tag" }
if (Test-Path -LiteralPath $StageDir) { throw "Output directory already exists; refusing to overwrite: $StageDir" }

$commit = Assert-CleanDetachedCheckout $repo ''
$tagTarget = (& git -C $repo rev-parse -q --verify "refs/tags/$tag^{commit}")
if ($LASTEXITCODE -ne 0 -or -not $tagTarget -or $tagTarget.Trim() -ne $commit) {
    throw "Missing or mismatched $tag Git tag; do NOT build an official release."
}
$tagExact = (& git -C $repo describe --tags --exact-match HEAD)
if ($LASTEXITCODE -ne 0 -or $tagExact.Trim() -ne $tag) { throw "HEAD must point exactly at the $tag tag." }
Assert-SourceVersion $repo $Version
$notes = Get-NotesPath $repo $Version
if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) { throw "Signing key not found: $KeyPath" }
Write-Host "Verified immutable release source: tag=$tag commit=$commit"

try {
    # Inside try: a cancelled or failed prompt still clears the variable in finally.
    Read-PassphraseIntoEnvironment
    & $Python (Join-Path $repo 'scripts\check_release_signing_key.py') $KeyPath $OfficialKeyId
    if ($LASTEXITCODE -ne 0) { throw 'Signing-key verification failed; nothing was staged.' }
    Write-Host "Staging official-tag candidate from commit $commit"
    & $Python (Join-Path $repo 'scripts\windows_installer.py') stage --private-key $KeyPath --notes $notes `
        --min-upgrade-from $MinUpgradeFrom --out $StageDir
    if ($LASTEXITCODE -ne 0) { throw 'Windows installer staging failed.' }
}
finally {
    Remove-Item Env:NATURE_RELEASE_KEY_PASSPHRASE -ErrorAction SilentlyContinue
}

$verified = Invoke-StagedPayloadVerification $Python $repo $StageDir
Assert-StagedBuild $StageDir $Version $commit $MinUpgradeFrom $tag $verified | Out-Null
if ($verified.verify_exit -ne 0 -or $verified.verify.verified_official_release -ne $true) {
    throw 'Official source/signature/integrity verification failed.'
}
Write-Host 'Verified tagged source, official manifest signature and artifact integrity.'

$compiler = Find-ISCC $ISCC
if (-not $compiler) {
    Write-Host "Staging succeeded. Inno Setup 6 ISCC.exe was not found. Compile later: ISCC.exe `"$StageDir\NatureAIWorker.iss`""
    exit 0
}
& $compiler (Join-Path $StageDir 'NatureAIWorker.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup compilation failed.' }
Write-InstallerDigests $StageDir | Out-Null
Write-Host 'Built locally. Publication still requires final binary acceptance, release-asset review, and explicit owner approval.'
