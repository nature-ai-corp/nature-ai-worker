# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
# Stage a PRIVATE Windows release candidate from an exact, untagged commit, signed with the
# owner's locally held release key. Nothing is tagged, pushed, uploaded or published.
#
#   git checkout --detach <commit>
#   powershell -File scripts\windows_rc_stage.ps1 -Version X.Y.Z -Commit <commit> [-Label rc1]
#
# The committed release notes stay final: the "private release candidate" banner is added only
# to the copy signed into this candidate, so the same commit can later be tagged unchanged.
# The signed manifest records no tag; release_bundle.py verify therefore reports this build as
# NOT an official release (only the tag checks fail), which this script checks.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$Commit,
    [string]$Label = 'rc1',
    [string]$MinUpgradeFrom = '0.1.0',
    [string]$KeyPath = (Join-Path $HOME 'NatureAI-Release-Keys\release-key.pem'),
    [string]$StageDir = '',
    [string]$ISCC = '',
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'windows_release_common.ps1')
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

Assert-ReleaseVersion $Version
Assert-ReleaseVersion $MinUpgradeFrom
if ($Commit -notmatch '^[0-9a-f]{40}$') { throw 'Commit must be a full 40-character lowercase SHA-1.' }
if ($Label -notmatch '^rc[0-9]{1,3}$') { throw "Label must look like rc1 (got '$Label')." }
if ($env:OS -ne 'Windows_NT') { throw 'This helper must run on Windows.' }
if (-not $Python) { $Python = Join-Path $repo '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python not found: $Python (create the release venv with py -3.13 -m venv .venv)."
}
if (-not $StageDir) { $StageDir = Join-Path $repo ("dist\windows-rc-{0}-{1}" -f $Version, $Label) }
if (Test-Path -LiteralPath $StageDir) { throw "Output directory already exists; refusing to overwrite: $StageDir" }

$head = Assert-CleanDetachedCheckout $repo $Commit
Assert-SourceVersion $repo $Version
& git -C $repo rev-parse -q --verify ("refs/tags/v{0}" -f $Version) | Out-Null
if ($LASTEXITCODE -eq 0) {
    throw "The official tag v$Version already exists; build it with windows_official_release_stage.ps1, not as a candidate."
}
$notes = Get-NotesPath $repo $Version
if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) { throw "Signing key not found: $KeyPath" }
Write-Host "Verified candidate source: version=$Version commit=$head label=$Label (untagged)"

# The candidate's signed notes: the final text with a banner that cannot be mistaken for a release.
$rcNotes = Join-Path ([IO.Path]::GetTempPath()) ("nature-worker-{0}-{1}-notes-{2}.md" -f $Version, $Label, [guid]::NewGuid())
$banner = "> **PRIVATE RELEASE CANDIDATE $Version-$Label - not an official release.** Built from commit $head; " +
          "no v$Version tag exists yet. Verify it only as a candidate.`r`n`r`n"
[IO.File]::WriteAllText($rcNotes, $banner + (Get-Content -LiteralPath $notes -Raw), (New-Object Text.UTF8Encoding $false))

Read-PassphraseIntoEnvironment
try {
    & $Python (Join-Path $repo 'scripts\check_release_signing_key.py') $KeyPath $OfficialKeyId
    if ($LASTEXITCODE -ne 0) { throw 'Signing-key verification failed; nothing was staged.' }
    Write-Host "Staging private candidate from commit $head"
    & $Python (Join-Path $repo 'scripts\windows_installer.py') stage --private-key $KeyPath --notes $rcNotes `
        --min-upgrade-from $MinUpgradeFrom --out $StageDir
    if ($LASTEXITCODE -ne 0) { throw 'Windows installer staging failed.' }
}
finally {
    Remove-Item Env:NATURE_RELEASE_KEY_PASSPHRASE -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $rcNotes -ErrorAction SilentlyContinue
}

$manifest = Assert-StagedBuild $StageDir $Version $head $MinUpgradeFrom ''
if ($manifest.notes -notmatch 'PRIVATE RELEASE CANDIDATE') { throw 'The candidate banner is missing from the signed notes.' }

# Signature, official key and repository must verify; only the tag checks may (and must) fail.
$report = & $Python (Join-Path $repo 'scripts\release_bundle.py') verify --bundle (Join-Path $StageDir 'payload\bundle') --repo $repo
$verifyExit = $LASTEXITCODE
$report = ($report -join "`n") | ConvertFrom-Json
$failed = @($report.checks | Where-Object { $_.ok -eq $false } | ForEach-Object { $_.check })
foreach ($required in 'signature_and_integrity', 'official_signer', 'official_repository') {
    $c = @($report.checks | Where-Object { $_.check -eq $required })
    if ($c.Count -ne 1 -or $c[0].ok -ne $true) { throw "Verification failed: $required." }
}
if ($verifyExit -eq 0 -or ($failed | Where-Object { $_ -notin 'tag_matches_version', 'tag_points_at_commit' })) {
    throw "Unexpected verification result for an untagged candidate: failed=[$($failed -join ', ')]."
}
Write-Host "Verified: signed by official key $($report.signer.key_id); manifest SHA-256 $($report.manifest_sha256); untagged (not an official release)."

$compiler = Find-ISCC $ISCC
$installers = @()
if ($compiler) {
    & $compiler (Join-Path $StageDir 'NatureAIWorker.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup compilation failed.' }
    $installers = Write-InstallerDigests $StageDir
} else {
    Write-Host "Inno Setup 6 ISCC.exe not found. Compile later: ISCC.exe `"$StageDir\NatureAIWorker.iss`""
}
$evidence = [ordered]@{
    kind = 'private-release-candidate'; version = $Version; label = $Label; commit = $head; tag = $null
    min_upgrade_from = $MinUpgradeFrom; manifest_sha256 = $report.manifest_sha256; signer = $report.signer.key_id
    verify_failed_checks = $failed; installers = $installers
}
$evidence | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $StageDir 'rc-evidence.json') -Encoding UTF8
Write-Host "Status: PRIVATE RELEASE CANDIDATE. Evidence: $(Join-Path $StageDir 'rc-evidence.json')"
Write-Host 'Publishing requires the official tag build (windows_official_release_stage.ps1) and explicit owner approval.'
