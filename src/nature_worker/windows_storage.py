# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Windows enterprise storage permission probe (read-only).

Never claims isolation merely because a path exists or data is encrypted.
Native installer must set and verify NTFS DACLs for actual deployment.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from .os_probes import ProbeFinding


def inspect_windows_storage_permissions(root: Path) -> ProbeFinding:
    if sys.platform != "win32":
        return ProbeFinding("windows_storage_acl", None, "Requires Windows")
    if root.is_symlink():
        return ProbeFinding("windows_storage_acl", False, "Enterprise directory is a link")
    if not root.is_dir():
        return ProbeFinding("windows_storage_acl", None, "Enterprise data root not provisioned")
    # os.stat()/chmod on Windows do not expose the full NTFS DACL or effective
    # access across user tokens. Never return verified clean without native ACL validation.
    try:
        root.stat()
    except OSError:
        return ProbeFinding("windows_storage_acl", None, "Cannot inspect enterprise directory")
    return ProbeFinding("windows_storage_acl", None,
                        "NTFS DACL/effective identity inspection not yet implemented")
