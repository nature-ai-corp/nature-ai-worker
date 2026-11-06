# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
# Shared checks for windows_rc_stage.ps1 and windows_official_release_stage.ps1 (dot-sourced).
# Every check runs before the signing passphrase is requested and fails closed: no version,
# tag, commit or notes file is assumed; each is given explicitly and compared with the source.

$OfficialKeyId = '64ecfc2da2ed12e9'
$OfficialRepository = 'https://github.com/nature-ai-corp/nature-ai-worker'

function Assert-ReleaseVersion([string]$Version) {
    if ($Version -notmatch '^\d+\.\d+\.\d+$') {
        throw "Version must be X.Y.Z (got '$Version'); pre-release labels belong to -Label, not the version."
    }
}

function Get-SourceVersions([string]$Repo) {
    # The versions the committed source declares; both must agree with the requested release.
    $init = Get-Content -LiteralPath (Join-Path $Repo 'src\nature_worker\__init__.py') -Raw
    $pyproject = Get-Content -LiteralPath (Join-Path $Repo 'pyproject.toml') -Raw
    $package = if ($init -match '(?m)^__version__ = "([^"]+)"') { $Matches[1] } else { '' }
    $project = if ($pyproject -match '(?m)^version = "([^"]+)"') { $Matches[1] } else { '' }
    return @{ package = $package; project = $project }
}

function Assert-SourceVersion([string]$Repo, [string]$Version) {
    $v = Get-SourceVersions $Repo
    if ($v.package -ne $Version -or $v.project -ne $Version) {
        throw "Source version mismatch: requested $Version, __init__.py $($v.package), pyproject.toml $($v.project)."
    }
}

function Assert-CleanDetachedCheckout([string]$Repo, [string]$Commit) {
    # A branch name is never trusted: the exact commit is checked out in detached HEAD mode.
    $branch = (& git -C $Repo branch --show-current)
    if ($LASTEXITCODE -ne 0) { throw 'Not a Git checkout.' }
    if ($branch -and $branch.Trim()) {
        throw "Check out the exact commit in detached HEAD mode (current branch: $($branch.Trim()))."
    }
    $dirty = & git -C $Repo status --porcelain
    if ($LASTEXITCODE -ne 0 -or $dirty) { throw 'Working tree is not clean; refusing to build.' }
    $head = (& git -C $Repo rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve Git HEAD.' }
    if ($Commit -and $head -ne $Commit) { throw "HEAD is $head, not the requested commit $Commit." }
    $shallow = (& git -C $Repo rev-parse --is-shallow-repository).Trim()
    if ($shallow -eq 'true') { throw 'Shallow clone: the release needs the full history.' }
    return $head
}

function Get-NotesPath([string]$Repo, [string]$Version) {
    $notes = Join-Path $Repo ("docs\RELEASE_NOTES_v{0}.md" -f $Version)
    if (-not (Test-Path -LiteralPath $notes -PathType Leaf)) { throw "Release notes not found: $notes" }
    if ((Get-Content -LiteralPath $notes -Raw) -match 'DRAFT|PRIVATE RC ONLY|NOT PUBLISHED|PRIVATE RELEASE CANDIDATE') {
        throw 'The committed release notes contain candidate-only wording; they must be the final text.'
    }
    return $notes
}

function Read-PassphraseIntoEnvironment {
    # Never on the command line, in history or in the repository: release_bundle.py reads it
    # from this process's environment, which is cleared by the caller in a finally block.
    $secure = Read-Host 'Release signing key passphrase (input hidden)' -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $env:NATURE_RELEASE_KEY_PASSPHRASE = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        $secure.Dispose()
    }
}

function Find-ISCC([string]$ISCC) {
    if ($ISCC) {
        if (-not (Test-Path -LiteralPath $ISCC -PathType Leaf)) { throw "ISCC.exe not found: $ISCC" }
        return $ISCC
    }
    foreach ($candidate in @(
            (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
            (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
            (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'))) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $candidate }
    }
    return ''
}

function Assert-StagedBuild([string]$StageDir, [string]$Version, [string]$Commit, [string]$MinUpgradeFrom,
                            [AllowNull()][string]$Tag) {
    # What was staged is exactly what was requested: version, commit, upgrade floor, no local
    # trust-key override, the official repository and the expected tag (none for a candidate).
    $info = Get-Content -LiteralPath (Join-Path $StageDir 'build-info.json') -Raw | ConvertFrom-Json
    if ($info.version -ne $Version) { throw "Staged version $($info.version), expected $Version." }
    if ($info.commit -ne $Commit) { throw "Staged commit $($info.commit), expected $Commit." }
    if ($info.trust_key) { throw 'A local trust-key override was staged; this is not a release build.' }
    if ($info.min_upgrade_from -ne $MinUpgradeFrom) {
        throw "Upgrade floor $($info.min_upgrade_from), expected $MinUpgradeFrom."
    }
    $manifestFile = Join-Path $StageDir 'payload\bundle\release.json'
    if (-not (Test-Path -LiteralPath $manifestFile -PathType Leaf)) { throw 'Signed release manifest not found.' }
    $manifest = Get-Content -LiteralPath $manifestFile -Raw | ConvertFrom-Json
    if ($manifest.version -ne $Version -or $manifest.source.commit -ne $Commit) {
        throw 'Manifest version or source commit mismatch.'
    }
    if ($manifest.source.repository -ne $OfficialRepository) {
        throw "Unexpected source repository in manifest: $($manifest.source.repository)"
    }
    # [string] turns $null into '': an empty -Tag means "no tag" (a candidate).
    $recorded = if ($manifest.source.tag) { [string]$manifest.source.tag } else { '' }
    if ($recorded -ne $Tag) {
        throw "Manifest source tag '$recorded', expected '$Tag' (empty means untagged)."
    }
    return $manifest
}

function Write-InstallerDigests([string]$StageDir) {
    $out = @()
    Get-ChildItem -LiteralPath (Join-Path $StageDir 'output') -Filter '*.exe' -File | ForEach-Object {
        $digest = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        Write-Host "Installer: $($_.FullName)"
        Write-Host "SHA256: $digest"
        $out += [ordered]@{ file = $_.Name; sha256 = $digest; size = $_.Length }
    }
    return ,$out
}
