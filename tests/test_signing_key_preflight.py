# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""The Windows RC signing preflight must work as a Python script, not -c code."""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_release_signing_key.py"


def test_signing_key_preflight_cli_handles_matching_and_wrong_key_id(tmp_path):
    key = Ed25519PrivateKey.generate()
    password = "test-only-passphrase"
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(password.encode()),
    )
    path = tmp_path / "release-key.pem"
    path.write_bytes(pem)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_id = hashlib.sha256(pub).hexdigest()[:16]

    def run(expected_id, passphrase=password):
        env = os.environ.copy()
        env.pop("NATURE_RELEASE_KEY_PASSPHRASE", None)
        if passphrase is not None:
            env["NATURE_RELEASE_KEY_PASSPHRASE"] = passphrase
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(path), expected_id],
            env=env, capture_output=True, text=True, timeout=20,
        )

    good = run(key_id)
    assert good.returncode == 0 and key_id in good.stdout
    bad_id = run("0" * 16)
    assert bad_id.returncode != 0 and key_id not in bad_id.stdout
    bad_passphrase = run(key_id, "wrong-passphrase")
    assert bad_passphrase.returncode != 0
    missing_passphrase = run(key_id, None)
    assert missing_passphrase.returncode != 0
    for result in (good, bad_id, bad_passphrase, missing_passphrase):
        assert password not in result.stdout + result.stderr
        assert "PRIVATE KEY" not in result.stdout + result.stderr
