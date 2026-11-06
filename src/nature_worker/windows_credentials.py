# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Windows S-C token vault using DPAPI; never shares Standalone credentials.

Not an OS isolation boundary: same-user processes may decrypt these values.
Installer must enforce separate Windows principal and NTFS DACL before use.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from .windows_dpapi import protect_for_current_user, unprotect_for_current_user


class PlatformCredentialError(RuntimeError):
    pass


class WindowsPlatformCredentialStore:
    def __init__(self, root: Path):
        if sys.platform != "win32":
            raise PlatformCredentialError("Windows S-C credential store requires Windows")
        if root.is_symlink() or not root.is_dir():
            raise PlatformCredentialError("Enterprise secure storage must be pre-provisioned")
        self.root = root.resolve(strict=True)
        self._root_input = root

    def _file(self, name: str) -> Path:
        if not name or not name.isascii() or not all(
            c.isalnum() or c in "_-" for c in name
        ):
            raise PlatformCredentialError("Invalid enterprise credential identifier")
        if self._root_input.is_symlink() or not self.root.is_dir():
            raise PlatformCredentialError("Unsafe enterprise credential root")
        path = self.root / (name + ".dpapi")
        if path.is_symlink():
            raise PlatformCredentialError("Unsafe enterprise credential path")
        return path

    def put(self, name: str, secret: bytes) -> None:
        if not isinstance(secret, bytes):
            raise PlatformCredentialError("Credential must be bytes")
        path = self._file(name)
        if path.exists():
            raise PlatformCredentialError("Existing credentials require explicit rotation")
        ciphertext = protect_for_current_user(secret)
        fd, tmp = tempfile.mkstemp(prefix=".sc-credential-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(ciphertext)
                stream.flush()
                os.fsync(stream.fileno())
            # Fail closed against unintended overwrite of an existing token.
            if path.exists() or path.is_symlink():
                raise PlatformCredentialError("Concurrent credential update refused")
            os.rename(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def get(self, name: str) -> bytes | None:
        path = self._file(name)
        try:
            return unprotect_for_current_user(path.read_bytes())
        except FileNotFoundError:
            return None

    def delete(self, name: str) -> None:
        # Deletion of the DPAPI blob is not secure erasure of SSD remnants.
        # Server-side token revocation remains mandatory.
        self._file(name).unlink(missing_ok=True)
