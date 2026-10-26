# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Standalone -> S-C transition commit primitive.

This is NOT an enterprise installer. The caller must perform connection,
authentication, OS security, S-C storage and runtime verification first.
Until all checks pass the persistent mode stays Standalone. No auto-fallback
to Standalone after a successful commit.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable

from .installation_mode import MODE_FILE, STANDALONE, PLATFORM_CLIENT, ModeError, read_mode


def commit_platform_transition(data_dir: Path, *, verify_ready: Callable[[], bool]) -> None:
    """Commit mode change only after fully successful preparation.

    Must run with an exclusive installation lock and no in-flight local Agent
    work, enforced by the future installer. The validation callback must
    verify a *real* connected and secured S-C runtime, not UI/config flags.
    Failure before atomic replace leaves the original Standalone marker intact.
    """
    if read_mode(data_dir) != STANDALONE:
        raise ModeError("Only an installed Standalone Worker may transition to S-C.")
    if verify_ready() is not True:
        raise ModeError("S-C preparation failed; Standalone remains active.")
    # Re-check immediately before committing; installer must additionally
    # enforce exclusive ownership to prevent races with concurrent processes.
    if read_mode(data_dir) != STANDALONE:
        raise ModeError("Installation mode changed during S-C preparation.")
    path = data_dir / MODE_FILE
    if path.is_symlink():
        raise ModeError("Unsafe installation marker.")
    payload = (json.dumps({"schema": 1, "mode": PLATFORM_CLIENT}) + "\n").encode()
    fd, temp = tempfile.mkstemp(prefix=".installation-mode-", dir=data_dir)
    try:
        if hasattr(os, 'fchmod'):
            os.fchmod(fd, 0o600)  # POSIX; Windows ACLs require installer enforcement
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
