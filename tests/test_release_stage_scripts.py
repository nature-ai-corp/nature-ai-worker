# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""The Windows release stage scripts: no hard-coded release, and fail-closed checks first.

Static checks run everywhere. On Windows, the real scripts run against a disposable Git
repository and must refuse every wrong state - before the signing passphrase is requested
(-NonInteractive: a prompt would fail the run instead of hanging it).
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
STAGE_SCRIPTS = ("windows_rc_stage.ps1", "windows_official_release_stage.ps1", "windows_release_preflight.ps1",
                 "windows_release_common.ps1")


def _text(name):
    return (SCRIPTS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", STAGE_SCRIPTS)
def test_no_release_version_or_tag_is_hard_coded(name):
    text = _text(name)
    assert not re.search(r"v\d+\.\d+\.\d+", text), "a release tag literal"
    # The only version literal allowed is the explicit, overridable upgrade-floor default.
    literals = re.findall(r"\b\d+\.\d+\.\d+\b", text)
    assert set(literals) <= {"0.1.0"} and text.count("0.1.0") <= 1
    assert "RELEASE_NOTES_v0" not in text and "release/v0" not in text


@pytest.mark.parametrize("name", ["windows_rc_stage.ps1", "windows_official_release_stage.ps1"])
def test_version_is_mandatory_and_every_check_precedes_the_passphrase(name):
    text = _text(name)
    assert "[Parameter(Mandatory = $true)][string]$Version" in text
    prompt = text.index("Read-PassphraseIntoEnvironment\n")
    for check in ("Assert-ReleaseVersion $Version", "Assert-CleanDetachedCheckout", "Assert-SourceVersion $repo $Version",
                  "Get-NotesPath $repo $Version", "Signing key not found"):
        assert text.index(check) < prompt, check
    assert "Remove-Item Env:NATURE_RELEASE_KEY_PASSPHRASE" in text[prompt:]
    assert "Assert-StagedBuild" in text[prompt:]


def test_candidate_is_untagged_and_must_verify_as_signed_but_not_official():
    text = _text("windows_rc_stage.ps1")
    assert "[Parameter(Mandatory = $true)][string]$Commit" in text
    assert "The official tag v$Version already exists" in text
    assert "Assert-StagedBuild $StageDir $Version $head $MinUpgradeFrom '' $verified" in text
    # The banner is checked in the verified notes text: the manifest's "notes" is only a path and hash,
    # so the former check of $manifest.notes could never pass.
    assert "PRIVATE RELEASE CANDIDATE" in text and "$manifest.notes" not in text
    assert '$verified.notes_text -cnotmatch "PRIVATE RELEASE CANDIDATE $Version-$Label"' in text
    assert '$verified.notes_text -notlike "*$head*"' in text
    for required in ("signature_and_integrity", "official_signer", "official_repository"):
        assert f"'{required}'" in text
    assert "'tag_matches_version', 'tag_points_at_commit'" in text


def test_official_build_requires_the_exact_tag_and_a_fully_verified_release():
    text = _text("windows_official_release_stage.ps1")
    assert '$tag = "v$Version"' in text and "refs/tags/$tag^{commit}" in text
    assert "describe --tags --exact-match HEAD" in text
    assert "Assert-StagedBuild $StageDir $Version $commit $MinUpgradeFrom $tag $verified" in text
    assert "$verified.verify_exit -ne 0 -or $verified.verify.verified_official_release -ne $true" in text
    assert "throw 'Official source/signature/integrity verification failed.'" in text


@pytest.mark.parametrize("name", ["windows_rc_stage.ps1", "windows_official_release_stage.ps1"])
def test_stage_scripts_verify_the_payload_setup_installs(name):
    # Staging keeps only payload.tar; the scripts once read payload\bundle, which no longer exists,
    # and failed after signing. They must verify the archive itself, with no local trust override.
    text = _text(name).replace("\r\n", "\n")
    assert "payload\bundle" not in text and "release_bundle.py') verify" not in text
    assert "$verified = Invoke-StagedPayloadVerification $Python $repo $StageDir\n" in text
    assert "-TrustKey" not in text and "--trust-key" not in text
    prompt = text.index("Read-PassphraseIntoEnvironment\n")
    assert text.index("Invoke-StagedPayloadVerification") > prompt


def test_staged_build_checks_read_no_unpacked_folder():
    common = _text("windows_release_common.ps1")
    assert "payload\bundle" not in common and "'verify-stage'" in common
    assert "$manifest = $Verified.manifest" in common
    assert "if ($exit -ne 0 -or -not $result.cleaned_up)" in common


def test_committed_notes_must_be_final_text():
    common = _text("windows_release_common.ps1")
    # Case-sensitive in PowerShell too (-cmatch), like the Python check below: -match ignores case
    # and refused the final v0.1.2 notes for the feature name "Draft Replies".
    assert "-cmatch 'DRAFT|PRIVATE RC ONLY|NOT PUBLISHED|PRIVATE RELEASE CANDIDATE'" in common
    assert "-match 'DRAFT" not in common
    assert '"docs\\RELEASE_NOTES_v{0}.md" -f $Version' in common


def test_release_notes_for_the_source_version_exist_and_are_final():
    import nature_worker
    notes = REPO / "docs" / f"RELEASE_NOTES_v{nature_worker.__version__}.md"
    text = notes.read_text(encoding="utf-8")
    assert not re.search(r"DRAFT|PRIVATE RC ONLY|NOT PUBLISHED|PRIVATE RELEASE CANDIDATE", text)
    assert f"v{nature_worker.__version__}" in text


# -- the real scripts on Windows ------------------------------------------------------------

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
windows_only = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="runs the PowerShell scripts on Windows")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def release_repo(tmp_path):
    """A disposable repository with the release scripts and a 1.2.3 source, at a detached commit."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in STAGE_SCRIPTS:
        shutil.copyfile(SCRIPTS / name, repo / "scripts" / name)
    (repo / "src" / "nature_worker").mkdir(parents=True)
    (repo / "src" / "nature_worker" / "__init__.py").write_text('__version__ = "1.2.3"\n')
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n')
    (repo / "docs").mkdir()
    (repo / "docs" / "RELEASE_NOTES_v1.2.3.md").write_text("# Nature AI Worker v1.2.3\n\nFinal notes.\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "release")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "--detach", head)
    return repo, head, tmp_path


def _run(repo, script, *args):
    proc = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                           str(repo / "scripts" / script), *args, "-Python", sys.executable,
                           "-KeyPath", str(repo.parent / "absent-key.pem")],
                          capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout + proc.stderr


@windows_only
def test_candidate_script_passes_its_checks_then_stops_at_the_missing_key(release_repo):
    repo, head, _ = release_repo
    code, out = _run(repo, "windows_rc_stage.ps1", "-Version", "1.2.3", "-Commit", head)
    assert code != 0 and "Signing key not found" in out and "passphrase" not in out.lower()


@windows_only
@pytest.mark.parametrize("change, args, message", [
    (None, ["-Version", "1.2.4"], "Source version mismatch"),
    (None, ["-Version", "1.2"], "Version must be X.Y.Z"),
    (None, ["-Version", "1.2.3-rc1"], "Version must be X.Y.Z"),
    (None, ["-Version", "1.2.3", "-Label", "final"], "Label must look like rc1"),
    ("branch", ["-Version", "1.2.3"], "detached HEAD"),
    ("dirty", ["-Version", "1.2.3"], "not clean"),
    ("tag", ["-Version", "1.2.3"], "official tag v1.2.3 already exists"),
    ("draft", ["-Version", "1.2.3"], "candidate-only wording"),
    ("pyproject", ["-Version", "1.2.3"], "Source version mismatch"),
])
def test_candidate_script_refuses_wrong_states(release_repo, change, args, message):
    repo, head, _ = release_repo
    commit = head
    if change == "branch":
        _git(repo, "checkout", "-q", "-b", "release/any")
    elif change == "dirty":
        (repo / "untracked.txt").write_text("x")
    elif change == "tag":
        _git(repo, "tag", "v1.2.3")
    elif change in ("draft", "pyproject"):
        target = repo / ("docs/RELEASE_NOTES_v1.2.3.md" if change == "draft" else "pyproject.toml")
        target.write_text(target.read_text().replace("Final notes.", "DRAFT notes.").replace('"1.2.3"', '"1.2.4"'))
        _git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qam", "change")
        commit = _git(repo, "rev-parse", "HEAD")
    code, out = _run(repo, "windows_rc_stage.ps1", *args, "-Commit", commit)
    assert code != 0 and message in out, out
    assert "Signing key not found" not in out  # refused before even looking for the key


@windows_only
def test_candidate_script_accepts_ordinary_words_that_contain_a_marker(release_repo):
    # The final v0.1.2 notes name the starter task "Draft Replies"; only the upper-case banners
    # (DRAFT, PRIVATE RC ONLY, NOT PUBLISHED, PRIVATE RELEASE CANDIDATE) mark candidate-only text.
    repo, _, _ = release_repo
    notes = repo / "docs" / "RELEASE_NOTES_v1.2.3.md"
    notes.write_text(notes.read_text() + "\nStarters: *Draft Replies*; drafts are not published by default.\n")
    _git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qam", "final wording")
    commit = _git(repo, "rev-parse", "HEAD")
    code, out = _run(repo, "windows_rc_stage.ps1", "-Version", "1.2.3", "-Commit", commit)
    assert code != 0 and "Signing key not found" in out, out  # every source check passed
    assert "candidate-only wording" not in out


@windows_only
def test_candidate_script_refuses_a_different_commit(release_repo):
    repo, head, _ = release_repo
    code, out = _run(repo, "windows_rc_stage.ps1", "-Version", "1.2.3", "-Commit", "0" * 40)
    assert code != 0 and "not the requested commit" in out


@windows_only
def test_official_script_requires_the_exact_tag(release_repo):
    repo, head, _ = release_repo
    code, out = _run(repo, "windows_official_release_stage.ps1", "-Version", "1.2.3")
    assert code != 0 and "Missing or mismatched v1.2.3 Git tag" in out
    _git(repo, "tag", "v1.2.3")
    code, out = _run(repo, "windows_official_release_stage.ps1", "-Version", "1.2.3")
    assert code != 0 and "Signing key not found" in out  # every source check passed
    code, out = _run(repo, "windows_official_release_stage.ps1", "-Version", "1.2.4")
    assert code != 0 and "Missing or mismatched v1.2.4 Git tag" in out
