"""Encrypted Platform Client cache primitive; no Standalone credential reuse.

The caller must obtain a dedicated Fernet key from an OS-protected store. This
module never generates a key on disk, reads Standalone provider credentials, or
grants the local Agent access to enterprise data. OS process isolation, key
provisioning and enterprise authorization are separate, mandatory release gates.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class PlatformStorageError(RuntimeError):
    pass


class EncryptedPlatformStore:
    def __init__(self, root: Path, key: bytes):
        if not isinstance(key, bytes):
            raise PlatformStorageError("An externally provisioned S-C key is required.")
        try:
            self._cipher = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise PlatformStorageError("Invalid S-C encryption key.") from exc
        self.root = root.resolve(strict=False)
        if root.is_symlink():
            raise PlatformStorageError("S-C storage root must not be a symlink.")
        self._root_input = root

    def _path(self, name: str) -> Path:
        if not isinstance(name, str) or not name or name in (".", ".."):
            raise PlatformStorageError("Invalid S-C cache name.")
        if not all(c.isascii() and (c.isalnum() or c in "_-.") for c in name):
            raise PlatformStorageError("Only simple S-C cache names are allowed.")
        if not name[0].isalnum():
            raise PlatformStorageError("Invalid S-C cache name.")
        return self.root / (name + ".enc")

    def read(self, name: str) -> bytes | None:
        path = self._path(name)
        if self._root_input.is_symlink() or path.is_symlink():
            raise PlatformStorageError("Unsafe S-C storage path.")
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            with os.fdopen(fd, "rb") as stream:
                ciphertext = stream.read()
            return self._cipher.decrypt(ciphertext)
        except FileNotFoundError:
            return None
        except (InvalidToken, OSError) as exc:
            raise PlatformStorageError("Cannot read or decrypt S-C cache.") from exc

    def write(self, name: str, value: bytes) -> None:
        path = self._path(name)
        if not isinstance(value, bytes):
            raise PlatformStorageError("S-C cache must contain bytes.")
        if self._root_input.is_symlink() or path.is_symlink():
            raise PlatformStorageError("Unsafe S-C storage path.")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._root_input.is_symlink():
            raise PlatformStorageError("Unsafe S-C storage path.")
        ciphertext = self._cipher.encrypt(value)
        fd, tmp = tempfile.mkstemp(prefix=".cache-", dir=self.root)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)  # POSIX only; Windows ACL is an installer requirement
            with os.fdopen(fd, "wb") as stream:
                stream.write(ciphertext)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
