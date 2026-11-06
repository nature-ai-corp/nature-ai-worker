# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Release identity: versions, trusted keys, signatures and bundle verification."""
import json
import os

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nature_worker.upgrade import release
from nature_worker.upgrade.release import (
    Manifest, ReleaseError, Version, load_keys, public_key_entry, sign_manifest, stage_bundle,
    trusted_keys, verify_bundle, wheel_requirement,
)
from upgrade_fixtures import make_bundle, trusted

KEY = Ed25519PrivateKey.generate()


def test_versions_compare_release_numbers_and_reject_paths():
    assert Version.parse("1.10.0") > Version.parse("1.9.9")
    assert Version.parse("0.1.0+g5f2cff6270eb") == Version.parse("0.1.0")  # a local build is not newer
    assert str(Version.parse("0.1.0+g1")) == "0.1.0+g1"
    for bad in ("1.0", "01.0.0", "1.0.0-rc1", "../1.0.0", "1.0.0/x", "1.0.0+UP", ""):
        with pytest.raises(ReleaseError):
            Version.parse(bad)


def test_registered_official_key_is_loaded():
    # The owner's official public key is registered; the private key must never be included.
    entries = json.loads(release.BUNDLED_KEYS.read_text())["keys"]
    assert [entry["key_id"] for entry in entries] == ["64ecfc2da2ed12e9"]
    assert [(key.key_id, key.scope) for key in trusted_keys()] == [
        ("64ecfc2da2ed12e9", "official")
    ]


def test_local_keys_are_never_official(tmp_path):
    entry = public_key_entry(KEY, "company build key")
    path = tmp_path / "trusted-keys.json"
    path.write_text(json.dumps({"schema": 1, "keys": [entry]}))
    keys = trusted_keys(path)
    assert [(k.key_id, k.scope) for k in keys if k.scope == "local"] == [
        (entry["key_id"], "local")
    ]
    assert [(k.key_id, k.scope) for k in keys if k.scope == "official"] == [
        ("64ecfc2da2ed12e9", "official")
    ]
    # A locally supplied copy of an official key cannot override its official scope.
    official_entry = json.loads(release.BUNDLED_KEYS.read_text())["keys"][0]
    path.write_text(json.dumps({"schema": 1, "keys": [entry, official_entry]}))
    keys = trusted_keys(path)
    assert sum(k.key_id == official_entry["key_id"] for k in keys) == 1
    assert next(k for k in keys if k.key_id == official_entry["key_id"]).scope == "official"
    entry["key_id"] = "0" * 16
    path.write_text(json.dumps({"schema": 1, "keys": [entry]}))
    with pytest.raises(ReleaseError, match="does not match"):
        load_keys(path, "local")


def test_valid_bundle_verifies(tmp_path):
    verified = verify_bundle(make_bundle(tmp_path, KEY, "1.2.3", notes="Better"), [trusted(KEY)])
    assert str(verified.manifest.version) == "1.2.3" and verified.notes == "Better"
    assert not verified.official and len(verified.wheels()) == 2


def test_signature_by_unknown_key_or_over_altered_manifest_is_refused(tmp_path):
    bundle = make_bundle(tmp_path, KEY, "1.2.3")
    with pytest.raises(ReleaseError, match="does not trust"):
        verify_bundle(bundle, [trusted(Ed25519PrivateKey.generate())])
    raw = (bundle / "release.json").read_bytes().replace(b'"1.2.3"', b'"1.2.4"')
    (bundle / "release.json").write_bytes(raw)
    with pytest.raises(ReleaseError, match="signature is invalid"):
        verify_bundle(bundle, [trusted(KEY)])


@pytest.mark.parametrize("damage", ["flip", "truncate", "remove", "symlink"])
def test_damaged_or_swapped_files_are_refused(tmp_path, damage):
    bundle = make_bundle(tmp_path, KEY, "1.2.3")
    wheel = next((bundle / "wheels").glob("dep-*"))
    data = wheel.read_bytes()
    if damage == "flip":
        wheel.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    elif damage == "truncate":
        wheel.write_bytes(data[:-1])
    elif damage == "remove":
        wheel.unlink()
    else:
        if os.name == "nt":
            pytest.skip("creating symbolic links needs extra privileges on Windows")
        outside = tmp_path / "outside.whl"
        outside.write_bytes(data)
        wheel.unlink()
        wheel.symlink_to(outside)
    with pytest.raises(ReleaseError):
        verify_bundle(bundle, [trusted(KEY)])


def _manifest(**changes) -> bytes:
    m = {"schema": 1, "product": "nature-ai-worker", "version": "1.0.0", "released_at": "x",
         "target": {"os": "linux", "arch": "x86_64", "python": "3.12"}, "editions": ["standalone"],
         "upgrade_from": {"min": "0.1.0"}, "data_schema": {"standalone": 1},
         "notes": {"path": "RELEASE_NOTES.md", "sha256": "0" * 64, "size": 1},
         "product_wheel": "wheels/nature_ai_worker-1.0.0-py3-none-any.whl",
         "artifacts": [{"path": "wheels/nature_ai_worker-1.0.0-py3-none-any.whl", "sha256": "0" * 64, "size": 1}]}
    m.update(changes)
    return json.dumps(m).encode()


@pytest.mark.parametrize("changes, message", [
    ({"product": "other"}, "not a nature-ai-worker release"),
    ({"editions": ["enterprise"]}, "unknown edition"),
    ({"product_wheel": "wheels/other.whl"}, "not among the hashed"),
    ({"artifacts": [{"path": "../../etc/passwd", "sha256": "0" * 64, "size": 1}]}, "Unsafe file name"),
    ({"artifacts": [{"path": "wheels/x.sh", "sha256": "0" * 64, "size": 1}]}, "must be a wheel"),
    ({"upgrade_from": {"min": "2.0.0"}}, "newer than the release"),
    ({"schema": 2}, "schema"),
])
def test_manifest_validation(changes, message):
    with pytest.raises(ReleaseError, match=message):
        Manifest.parse(_manifest(**changes))


def test_staging_copies_only_listed_files_and_verifies_the_copy(tmp_path):
    bundle = make_bundle(tmp_path, KEY, "1.2.3")
    (bundle / "wheels" / "extra-9.9-py3-none-any.whl").write_bytes(b"not in manifest")
    staged = stage_bundle(str(bundle), tmp_path / "staging", [trusted(KEY)])
    assert staged.bundle == tmp_path / "staging"
    assert sorted(p.name for p in (tmp_path / "staging" / "wheels").iterdir()) == sorted(
        a.path.split("/")[1] for a in staged.manifest.artifacts)
    with pytest.raises(ReleaseError, match="local directory or an https"):
        stage_bundle("http://example.com/b", tmp_path / "s2", [trusted(KEY)])


def test_https_source_is_downloaded_then_verified(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, KEY, "1.2.3")
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        name = request.url.path.split("/release/", 1)[1]
        return httpx.Response(200, content=(bundle / name).read_bytes())

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler),
                                                           **{k: v for k, v in kw.items() if k != "trust_env"}))
    staged = stage_bundle("https://releases.example/release", tmp_path / "dl", [trusted(KEY)])
    assert str(staged.manifest.version) == "1.2.3"
    assert requested[:2] == ["/release/release.json", "/release/release.json.sig"]


def test_wheel_requirement():
    assert wheel_requirement("wheels/pydantic_core-2.50.1-cp312-cp312-manylinux_2_17_x86_64.whl") == \
        "pydantic_core==2.50.1"
    with pytest.raises(ReleaseError):
        wheel_requirement("wheels/evil.tar.gz")


def test_signature_round_trip():
    raw = b'{"x": 1}'
    signer = release.verify_signature(raw, sign_manifest(raw, KEY), [trusted(KEY)])
    assert signer.scope == "local"
