# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""OS helpers shared by tests, so the same assertions hold on Linux and Windows."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_NO_SYMLINK_PRIVILEGE = 1314  # ERROR_PRIVILEGE_NOT_HELD


def try_link(target: Path, link: Path, *, directory: bool = False) -> bool:
    """Create a symbolic link; False only when Windows withholds the privilege."""
    try:
        os.symlink(target, link, target_is_directory=directory)
        return True
    except OSError as exc:
        if sys.platform == "win32" and getattr(exc, "winerror", None) == _NO_SYMLINK_PRIVILEGE:
            return False
        raise


def make_link(target: Path, link: Path, *, directory: bool = False) -> None:
    """Create a symbolic link, or skip a test that cannot run without one."""
    if not try_link(target, link, directory=directory):
        pytest.skip("creating symbolic links needs Developer Mode or administrator rights on Windows")


def _windows_acl(path: Path) -> tuple[bool, set[str]]:
    literal = "'" + str(path).replace("'", "''") + "'"  # a PowerShell single-quoted string
    script = (f"$a = Get-Acl -LiteralPath {literal}; "
              "'protected=' + $a.AreAccessRulesProtected; "
              "$a.Access | ForEach-Object { $_.IdentityReference.Translate("
              "[System.Security.Principal.SecurityIdentifier]).Value }")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, check=True, timeout=60).stdout.split()
    return out[0] == "protected=True", set(out[1:])


def assert_private(path: Path) -> None:
    """Only this account (and, on Windows, SYSTEM and Administrators) can access ``path``."""
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o077 == 0, f"{path} is accessible to other accounts"
        return
    from nature_worker.config import current_user_sid
    from nature_worker.windows_security import read_acl
    # Use the same native Windows ACL parser as installation validation.
    # PowerShell Get-Acl's printed Access entries are inconsistent on CI and
    # can return an empty list even for a directory with a valid DACL.
    acl = read_acl(path)
    sids = acl.grantees()
    allowed = {current_user_sid(), "S-1-5-18", "S-1-5-32-544"}
    assert sids and sids <= allowed, f"{path} grants access to {sorted(sids - allowed)}"
    if path.is_dir():
        assert acl.protected, f"{path} still inherits its permissions from its parent folder"
