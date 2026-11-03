# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Runtime configuration read from environment variables.

Only deployment settings live here (paths, bind address). User-facing
settings such as the selected provider are stored in SQLite, and API keys
are stored by ``credentials.py``.
"""

from __future__ import annotations

import csv
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass
class Config:
    data_dir: Path
    workspace_dir: Path
    host: str = "127.0.0.1"
    port: int = 8080
    allowed_hosts: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Config":
        allowed = _env("NATURE_WORKER_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")
        return cls(
            data_dir=Path(_env("NATURE_WORKER_DATA_DIR", "./data")).resolve(),
            workspace_dir=Path(_env("NATURE_WORKER_WORKSPACE", "./workspace")).resolve(),
            host=_env("NATURE_WORKER_HOST", "127.0.0.1"),
            port=int(_env("NATURE_WORKER_PORT", "8080")),
            allowed_hosts=[h.strip() for h in allowed.split(",") if h.strip()],
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            # chmod cannot protect anything on Windows: the data root (master key,
            # encrypted API keys, backups) would inherit e.g. "Users: read" from C:\.
            try:
                restrict_to_owner(self.data_dir)
            except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
                raise PermissionError(
                    f"Cannot secure Nature AI Worker's private data directory {self.data_dir}. "
                    "Startup stopped rather than exposing credentials or history. "
                    "Choose a data directory owned by the current Windows user and check its ACL."
                ) from exc
            return
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass


# Well-known SIDs: LocalSystem, BUILTIN\Administrators, and the broad groups removed.
_SYSTEM, _ADMINISTRATORS = "S-1-5-18", "S-1-5-32-544"
_BROAD = ("S-1-1-0", "S-1-5-11", "S-1-5-32-545")  # Everyone, Authenticated Users, Users


def current_user_sid() -> str:
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                         check=True, timeout=30, stdin=subprocess.DEVNULL).stdout
    sid = next(csv.reader([out.strip()]))[-1]
    if not sid.startswith("S-1-"):
        raise ValueError("could not determine the current user's SID")
    return sid


def restrict_to_owner(path: Path) -> None:
    """Windows: an explicit, non-inherited ACL on ``path`` for this user, SYSTEM and
    Administrators only - the same principals as the user's own profile folder,
    and the Windows equivalent of POSIX mode 0700. Files inside inherit it."""
    # icacls /inheritance:r + /grant:r replaces grants only for named SIDs;
    # it can leave other explicit grants (including OWNER RIGHTS) on the root.
    # Replace the entire DACL, then verify the effective explicit grantees.
    from .windows_security import (
        ADMINISTRATORS, FILE_ALL_ACCESS, SYSTEM, read_acl, set_protected_dacl,
    )
    user = current_user_sid()
    set_protected_dacl(
        path, user,
        [(user, FILE_ALL_ACCESS), (SYSTEM, FILE_ALL_ACCESS),
         (ADMINISTRATORS, FILE_ALL_ACCESS)],
    )
    acl = read_acl(path)
    expected = {user, SYSTEM, ADMINISTRATORS}
    if not acl.protected or acl.grantees() != expected:
        raise PermissionError(
            f"Data directory access control is not private: "
            f"grantees={sorted(acl.grantees())!r}"
        )
