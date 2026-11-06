# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Verify a passphrase-encrypted Ed25519 release private key against a public key ID.

The passphrase comes from NATURE_RELEASE_KEY_PASSPHRASE in the calling process's
temporary environment. Never print or transmit private-key bytes or the passphrase.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def check_signing_key(path: Path, expected_key_id: str) -> str:
    secret = os.environ.get("NATURE_RELEASE_KEY_PASSPHRASE")
    if not secret:
        raise ValueError("Signing key passphrase is missing")
    key = serialization.load_pem_private_key(path.read_bytes(), password=secret.encode())
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Signing key is not Ed25519")
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_id = hashlib.sha256(pub).hexdigest()[:16]
    if key_id != expected_key_id:
        raise ValueError("Signing key ID does not match the registered official release key")
    return key_id


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("private_key", type=Path)
    p.add_argument("expected_key_id")
    args = p.parse_args()
    try:
        key_id = check_signing_key(args.private_key, args.expected_key_id)
    except (ValueError, TypeError, OSError) as exc:
        # Never print the exception from PEM loading: its text can vary with backends.
        print(f"Signing-key preflight refused: {type(exc).__name__}")
        return 1
    print("Signing key ID:", key_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
