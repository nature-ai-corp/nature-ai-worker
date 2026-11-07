# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Verifying a staged Windows build by the payload Setup installs (``windows_installer.py verify-stage``).

Staging keeps only ``payload.tar``. The release scripts once verified ``payload\\bundle``, which
staging had already deleted, so an official run failed after signing. These tests build stages
the way ``stage`` does - the same tar writer, a signed bundle with a runtime, build-info.json and
defines.iss - and check that verification accepts exactly a consistent, safe, signed payload,
refuses everything else, never writes outside its temporary folder, and always removes it.

``test_real_stage_then_verify_end_to_end`` runs the real ``stage`` (pinned runtime and 42 pinned
wheels) when NATURE_WORKER_STAGING_CACHE names a folder holding them (``wheels\\`` and the runtime
archive), so it needs no network; otherwise it is skipped.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nature_worker.upgrade.release import key_id_for, sign_manifest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
import windows_installer  # noqa: E402

BOOTSTRAP = REPO / "packaging" / "windows" / "bootstrap.py"
OFFICIAL_REPOSITORY = "https://github.com/nature-ai-corp/nature-ai-worker"
COMMIT = "a" * 40
RUNTIME = "runtime/cpython-3.12.15-20261009-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _key_file(key: Ed25519PrivateKey, path: Path) -> Path:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    path.write_text(json.dumps({"schema": 1, "keys": [{"key_id": key_id_for(raw), "comment": "test only",
                                                        "public_key": base64.b64encode(raw).decode()}]}))
    return path


def _bundle(payload: Path, key: Ed25519PrivateKey) -> dict:
    """A signed Windows Standalone bundle like release_bundle.py writes: wheels, notes, runtime."""
    bundle = payload / "bundle"
    (bundle / "wheels").mkdir(parents=True)
    (bundle / "runtime").mkdir()
    wheels = ["nature_ai_worker-1.2.3-py3-none-any.whl", "dep-1.0-py3-none-any.whl"]
    for name in wheels:
        (bundle / "wheels" / name).write_bytes(hashlib.sha256(name.encode()).digest() * 50)
    (bundle / "RELEASE_NOTES.md").write_text("# Nature AI Worker 1.2.3\n\nTest notes.\n")
    (bundle / RUNTIME).write_bytes(b"runtime archive bytes" * 100)

    def entry(rel: str) -> dict:
        data = (bundle / rel).read_bytes()
        return {"path": rel, "sha256": _sha(data), "size": len(data)}

    manifest = {
        "schema": 1, "product": "nature-ai-worker", "version": "1.2.3", "released_at": "2026-11-06",
        "channel": "stable", "target": {"os": "windows", "arch": "x86_64", "python": "3.12"},
        "editions": ["standalone"], "upgrade_from": {"min": "0.1.0"},
        "data_schema": {"standalone": 1, "platform_client": 1}, "client_interface": "1.3",
        "notes": entry("RELEASE_NOTES.md"), "product_wheel": f"wheels/{wheels[0]}",
        "artifacts": [entry(f"wheels/{w}") for w in wheels],
        "source": {"repository": OFFICIAL_REPOSITORY, "commit": COMMIT, "tag": None},
        "runtime": {**entry(RUNTIME), "python": "3.12.15", "origin": {"url": "https://example.invalid/rt",
                                                                       "sha256": entry(RUNTIME)["sha256"],
                                                                       "release": "20261009"}},
    }
    raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    (bundle / "release.json").write_bytes(raw)
    (bundle / "release.json.sig").write_bytes(sign_manifest(raw, key))
    return {"manifest_sha256": _sha(raw), "runtime": manifest["runtime"]}


def _records(out: Path, built: dict, *, trust_key: bool = False) -> None:
    """build-info.json and defines.iss as ``stage`` writes them, for the current payload.tar."""
    payload_sha = windows_installer.sha256(out / "payload.tar")
    runtime = built["runtime"]
    (out / "defines.iss").write_text(
        f'#define AppVersion "1.2.3"\n#define PayloadSha256 "{payload_sha}"\n'
        f'#define RuntimePath "{runtime["path"].replace("/", chr(92))}"\n'
        f'#define RuntimeSha256 "{runtime["sha256"]}"\n#define TrustKey {int(trust_key)}\n', encoding="utf-8")
    (out / "build-info.json").write_text(json.dumps({
        "version": "1.2.3", "commit": COMMIT, "test_build": False, "payload_sha256": payload_sha,
        "manifest_sha256": built["manifest_sha256"], "min_upgrade_from": "0.1.0", "wheels": 2,
        "trust_key": trust_key}, indent=2) + "\n")


@pytest.fixture
def key():
    return Ed25519PrivateKey.generate()


@pytest.fixture
def staged(tmp_path, key):
    """A consistent staged build signed by a test key, plus the key list that trusts it."""
    out = tmp_path / "stage"
    (out / "payload").mkdir(parents=True)
    built = _bundle(out / "payload", key)
    shutil.copyfile(BOOTSTRAP, out / "payload" / "bootstrap.py")
    windows_installer.write_payload_tar(out)
    shutil.rmtree(out / "payload")  # as stage does: only payload.tar remains
    _records(out, built)
    work = tmp_path / "work"
    work.mkdir()
    return out, built, _key_file(key, tmp_path / "test-keys.json"), work


def _verify(stage: Path, work: Path, trust: Path | None = None, repo: Path | None = None):
    args = [sys.executable, str(SCRIPTS / "windows_installer.py"), "verify-stage", "--stage", str(stage),
            "--work-dir", str(work)]
    if trust:
        args += ["--trust-key", str(trust)]
    if repo:
        args += ["--repo", str(repo)]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
    return proc.returncode, json.loads(proc.stdout)


def _check(report: dict, name: str) -> dict:
    return next(c for c in report.get("verify", report)["checks"] if c["check"] == name)


def _rewrite(stage: Path, built: dict, change=None, extra=()) -> None:
    """Rewrite payload.tar - optionally changing regular members (``change(name, data)``) and
    appending ``extra`` members - and the build records to match it: an attacker who controls the
    whole stage folder, so only the payload's own checks can object."""
    with tarfile.open(stage / "payload.tar") as src:
        members = [(m, src.extractfile(m).read() if m.isreg() else None) for m in src.getmembers()]
    if change:
        members = [(m, change(m.name, data) if data is not None else None) for m, data in members]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as dst:
        for info, data in [*members, *extra]:
            if data is not None:
                info.size = len(data)
            dst.addfile(info, io.BytesIO(data) if data is not None else None)
    (stage / "payload.tar").write_bytes(buffer.getvalue())
    _records(stage, built)


def _file(name: str) -> tuple[tarfile.TarInfo, bytes]:
    return tarfile.TarInfo(name), b"planted"


def _special(name: str, kind: bytes, link: str = "") -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, link
    return info, None


# -- the consistent case -------------------------------------------------------------------

def test_a_consistent_stage_verifies_from_its_payload_and_cleans_up(staged):
    stage, built, trust, work = staged
    code, report = _verify(stage, work, trust)
    assert code == 0, report
    names = [c["check"] for c in report["checks"]]
    assert names == ["payload_matches_build_info", "payload_matches_installer", "payload_extracted_safely",
                     "manifest_matches_build_info", "runtime_matches_installer", "bootstrap_matches_source",
                     "trust_key_matches_build_info", "no_private_key"]
    assert all(c["ok"] for c in report["checks"])
    assert report["payload_sha256"] == windows_installer.sha256(stage / "payload.tar")
    assert report["manifest"]["source"]["commit"] == COMMIT and report["manifest"]["version"] == "1.2.3"
    assert _check(report, "signature_and_integrity")["ok"] is True
    assert _check(report, "official_signer")["ok"] is False  # a test key is never official
    assert report["verify_exit"] == 1                      # not an official release
    assert report["cleaned_up"] is True and not any(work.iterdir())
    assert not (stage / "payload").exists()                # nothing is unpacked into the stage


def test_without_a_trusted_key_the_signature_fails_and_the_folder_is_still_removed(staged):
    stage, _, _, work = staged
    code, report = _verify(stage, work)
    assert code == 1 and "signature_and_integrity" in report["error"]
    assert report["cleaned_up"] is True and not any(work.iterdir())


# -- inconsistent stages: refused before anything is extracted ----------------------------------

@pytest.mark.parametrize("change, check", [
    ("payload", "payload_matches_build_info"),
    ("defines", "payload_matches_installer"),
    ("build-info", "payload_matches_build_info"),
])
def test_a_payload_that_differs_from_the_build_records_is_refused(staged, change, check):
    stage, _, trust, work = staged
    if change == "payload":
        with open(stage / "payload.tar", "ab") as tar:
            tar.write(b"\0" * 512)
    elif change == "defines":
        text = (stage / "defines.iss").read_text(encoding="utf-8")
        (stage / "defines.iss").write_text(text.replace('PayloadSha256 "', 'PayloadSha256 "0'), encoding="utf-8")
    else:
        info = json.loads((stage / "build-info.json").read_text())
        (stage / "build-info.json").write_text(json.dumps({**info, "payload_sha256": "0" * 64}))
    code, report = _verify(stage, work, trust)
    assert code == 2 and report["error"].startswith(check)
    assert "work_dir" not in report and not any(work.iterdir())  # nothing was extracted


@pytest.mark.parametrize("missing", ["payload.tar", "build-info.json", "defines.iss"])
def test_a_stage_with_a_missing_record_is_refused(staged, missing):
    stage, _, trust, work = staged
    (stage / missing).unlink()
    code, report = _verify(stage, work, trust)
    assert code == 2 and report["error"]


# -- unsafe archives: refused as a whole, nothing written outside the temporary folder -------------

UNSAFE = {
    "parent traversal": [_file("../escaped.txt")],
    "traversal inside payload": [_file("payload/../../escaped.txt")],
    "absolute path": [_file("/escaped.txt")],
    "drive-qualified path": [_file("C:/escaped.txt")],
    "backslash path": [_file("payload\\..\\..\\escaped.txt")],
    "outside payload": [_file("escaped.txt")],
    "alternate data stream": [_file("payload/bootstrap.py:stream")],
    "device name": [_file("payload/CON.txt")],
    "trailing dot": [_file("payload/name.")],
    "symlink": [_special("payload/link", tarfile.SYMTYPE, "../../escaped.txt")],
    "symlink then write through it": [_special("payload/ln", tarfile.SYMTYPE, ".."), _file("payload/ln/escaped.txt")],
    "hard link": [_special("payload/hard", tarfile.LNKTYPE, "payload/bootstrap.py")],
    "character device": [_special("payload/dev", tarfile.CHRTYPE)],
    "fifo": [_special("payload/fifo", tarfile.FIFOTYPE)],
    "duplicate member": [_file("payload/bootstrap.py")],
    "case-only duplicate": [_file("payload/BOOTSTRAP.py")],
}


@pytest.mark.parametrize("case", sorted(UNSAFE))
def test_an_unsafe_payload_archive_is_refused_entirely(staged, tmp_path, case):
    stage, built, trust, work = staged
    _rewrite(stage, built, extra=UNSAFE[case])
    code, report = _verify(stage, work, trust)
    assert code == 2, report
    assert "payload.tar member" in report["error"] or "more than once" in report["error"]
    assert report["cleaned_up"] is True and not any(work.iterdir())
    planted = [p for p in tmp_path.rglob("*") if p.is_file() and p.name in ("escaped.txt", "name.")]
    assert planted == []


def test_extraction_never_writes_into_a_folder_that_is_not_empty(tmp_path, staged):
    stage = staged[0]
    dest = tmp_path / "busy"
    dest.mkdir()
    (dest / "existing").write_text("x")
    with pytest.raises(windows_installer.StageError, match="not empty"):
        windows_installer.extract_payload(stage / "payload.tar", dest)


# -- a consistent but wrong payload -------------------------------------------------------------

def test_a_tampered_wheel_fails_integrity_even_when_the_records_were_rewritten(staged):
    stage, built, trust, work = staged
    flip = lambda name, data: data[:-1] + bytes([data[-1] ^ 1]) if name.endswith("dep-1.0-py3-none-any.whl") else data
    _rewrite(stage, built, change=flip)
    code, report = _verify(stage, work, trust)
    assert code == 1 and "SHA-256" in _check(report, "signature_and_integrity")["detail"]
    assert report["cleaned_up"] is True and not any(work.iterdir())


@pytest.mark.parametrize("what, check", [
    ("manifest", "manifest_matches_build_info"),
    ("runtime", "runtime_matches_installer"),
    ("bootstrap", "bootstrap_matches_source"),
    ("trust key", "trust_key_matches_build_info"),
    ("private key", "no_private_key"),
])
def test_a_payload_that_does_not_match_the_build_records_is_refused(staged, what, check):
    stage, built, trust, work = staged
    if what == "manifest":
        built = {**built, "manifest_sha256": "0" * 64}
        _records(stage, built)
    elif what == "runtime":
        _records(stage, {**built, "runtime": {**built["runtime"], "sha256": "0" * 64}})
    elif what == "bootstrap":
        _rewrite(stage, built, change=lambda name, data: b"print('not the shipped bootstrap')"
                 if name == "payload/bootstrap.py" else data)
    elif what == "trust key":
        _rewrite(stage, built, extra=[(tarfile.TarInfo("payload/trusted-dev-key.json"), b"{}")])
    else:
        marker = b"-----BEGIN " + b"PRIVATE KEY-----"  # split: the repository's secret scan
        _rewrite(stage, built, extra=[(tarfile.TarInfo("payload/notes.txt"), marker)])
    code, report = _verify(stage, work, trust)
    assert code == 2, report
    assert report["error"].startswith(check), report["error"]
    assert report["cleaned_up"] is True and not any(work.iterdir())


# -- the PowerShell helpers use exactly this report ---------------------------------------------

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
windows_only = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="runs the PowerShell helpers on Windows")


def _ps(script: str) -> tuple[int, str]:
    common = SCRIPTS / "windows_release_common.ps1"
    proc = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                           f"$ErrorActionPreference = 'Stop'; . '{common}'; {script}"],
                          capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout + proc.stderr


@windows_only
def test_powershell_helpers_accept_the_verified_payload(staged):
    stage, _, trust, _ = staged
    code, out = _ps(f"$v = Invoke-StagedPayloadVerification '{sys.executable}' '{REPO}' '{stage}' '{trust}'; "
                    f"$m = Assert-StagedBuild '{stage}' '1.2.3' '{COMMIT}' '0.1.0' '' $v; "
                    "'VERSION=' + $m.version; 'SIGNED=' + ($v.verify.checks | Where-Object check -eq "
                    "'signature_and_integrity').ok; 'CLEAN=' + $v.cleaned_up")
    assert code == 0, out
    assert "VERSION=1.2.3" in out and "SIGNED=True" in out and "CLEAN=True" in out


@windows_only
@pytest.mark.parametrize("args, message", [
    ("'1.2.4' '{commit}' '0.1.0' ''", "Staged version 1.2.3, expected 1.2.4"),
    ("'1.2.3' '{other}' '0.1.0' ''", "expected " + "b" * 40),
    ("'1.2.3' '{commit}' '0.1.1' ''", "Upgrade floor 0.1.0, expected 0.1.1"),
    ("'1.2.3' '{commit}' '0.1.0' 'v1.2.3'", "expected 'v1.2.3'"),
])
def test_powershell_build_checks_refuse_a_different_request(staged, args, message):
    stage, _, trust, _ = staged
    request = args.format(commit=COMMIT, other="b" * 40)
    code, out = _ps(f"$v = Invoke-StagedPayloadVerification '{sys.executable}' '{REPO}' '{stage}' '{trust}'; "
                    f"Assert-StagedBuild '{stage}' {request} $v")
    assert code != 0 and message in out, out


@windows_only
def test_powershell_helper_refuses_an_unsigned_or_unsafe_payload(staged):
    stage, built, _, _ = staged
    code, out = _ps(f"Invoke-StagedPayloadVerification '{sys.executable}' '{REPO}' '{stage}'")
    assert code != 0 and "Staged payload verification failed (exit 1)" in out and "signature_and_integrity" in out
    _rewrite(stage, built, extra=[_file("../escaped.txt")])
    code, out = _ps(f"Invoke-StagedPayloadVerification '{sys.executable}' '{REPO}' '{stage}'")
    assert code != 0 and "Staged payload verification failed (exit 2)" in out and "payload.tar member" in out


@windows_only
def test_powershell_build_check_requires_a_verified_report(staged):
    stage = staged[0]
    code, out = _ps(f"Assert-StagedBuild '{stage}' '1.2.3' '{COMMIT}' '0.1.0' '' $null")
    assert code != 0 and "No verified payload manifest was supplied" in out


# -- the real staging, end to end (pinned inputs from a local cache; no network) ----------------

CACHE = os.environ.get("NATURE_WORKER_STAGING_CACHE")


@pytest.mark.skipif(not CACHE, reason="set NATURE_WORKER_STAGING_CACHE to a folder with the pinned "
                                      "runtime archive and wheels\\ to run the real staging")
def test_real_stage_then_verify_end_to_end(tmp_path, key):
    cache = Path(CACHE)
    runtime = next(cache.glob("cpython-*.tar.gz"))
    pem = tmp_path / "keys" / "test-release-key.pem"  # outside the stage; never copied into it
    pem.parent.mkdir()
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    trust = _key_file(key, tmp_path / "keys" / "test-keys.json")
    out = tmp_path / "stage"
    env = {k: v for k, v in os.environ.items() if k != "NATURE_RELEASE_KEY_PASSPHRASE"}
    env.update(PIP_NO_INDEX="1", PIP_FIND_LINKS=str(cache / "wheels"))
    staged = subprocess.run([sys.executable, str(SCRIPTS / "windows_installer.py"), "stage", "--private-key", str(pem),
                             "--notes", str(REPO / "README.md"), "--min-upgrade-from", "0.1.0",
                             "--runtime", str(runtime), "--out", str(out)],
                            capture_output=True, text=True, env=env, timeout=900)
    assert staged.returncode == 0, staged.stdout[-2000:] + staged.stderr[-2000:]
    assert sorted(p.name for p in out.iterdir()) == ["NatureAIWorker.iss", "build-info.json", "defines.iss",
                                                     "licenses", "payload.tar"]
    info = json.loads((out / "build-info.json").read_text())
    work = tmp_path / "work"
    work.mkdir()
    code, report = _verify(out, work, trust, repo=REPO)
    assert code == 0, report.get("error")
    assert all(c["ok"] for c in report["checks"])
    assert report["payload_sha256"] == info["payload_sha256"]
    assert report["manifest"]["source"]["commit"] == info["commit"]
    assert report["manifest"]["runtime"]["sha256"] == json.loads(
        (REPO / "packaging" / "windows" / "runtime.json").read_text())["sha256"]
    assert len(report["manifest"]["artifacts"]) == 43
    assert _check(report, "signature_and_integrity")["ok"] is True
    assert _check(report, "official_signer")["ok"] is False
    assert report["cleaned_up"] is True and not any(work.iterdir())
    if POWERSHELL and os.name == "nt":
        code, ps_out = _ps(f"$v = Invoke-StagedPayloadVerification '{sys.executable}' '{REPO}' '{out}' '{trust}'; "
                           f"$m = Assert-StagedBuild '{out}' '{info['version']}' '{info['commit']}' '0.1.0' '' $v; "
                           "'STAGED=' + $m.version")
        assert code == 0 and f"STAGED={info['version']}" in ps_out, ps_out
