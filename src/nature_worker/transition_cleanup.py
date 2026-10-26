# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Failure cleanup coordinator for an attempted Standalone -> S-C upgrade.

This does not itself delete enterprise data. The installer must supply a
trusted, scoped cleanup and verification action for its temporary S-C staging
area, sessions and OS credentials. Never remove the user's Standalone files.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from .installation_mode import ModeError, STANDALONE, PLATFORM_CLIENT, read_mode

FAILED_CLEANUP_MARKER = "sc-transition-cleanup-required"


def require_no_failed_sc_residue(data_dir: Path) -> None:
    """Deny Standalone local-Agent access while failed S-C staging is unresolved."""
    marker = data_dir / FAILED_CLEANUP_MARKER
    if marker.is_symlink() or marker.exists():
        raise ModeError("Failed S-C transition may retain enterprise data; cleanup verification required.")


def attempt_platform_transition(
    data_dir: Path,
    *,
    prepare: Callable[[], bool],
    cleanup_and_verify: Callable[[], bool],
    commit: Callable[[], None],
) -> None:
    """Prepare S-C, commit only when ready; clean any failed preparation.

    The cleanup callback must revoke temporary sessions/credentials, securely
    remove ephemeral S-C data and verify absence. If cleanup cannot be proved,
    retain a marker and block local Standalone execution until remediation.
    The trusted installer must also handle abrupt process crashes via its
    durable staging journal before permitting Standalone to run.
    """
    if read_mode(data_dir) != STANDALONE:
        raise ModeError("Transition requires an installed Standalone.")
    require_no_failed_sc_residue(data_dir)
    marker = data_dir / FAILED_CLEANUP_MARKER
    # Write before prepare: interruption leaves evidence for next launch.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(marker, flags, 0o600)
    except FileExistsError as exc:
        raise ModeError("S-C transition already pending or cleanup required.") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("S-C transition pending: inspect and clean staging\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if prepare() is not True:
            raise ModeError("S-C preparation unsuccessful.")
        commit()
        if read_mode(data_dir) != PLATFORM_CLIENT:
            raise ModeError("S-C commit did not activate Platform Client mode.")
    except BaseException:
        # Atomic mode commit may have succeeded just before an exception.
        # Never destroy committed enterprise state as if it were staging.
        if read_mode(data_dir) == PLATFORM_CLIENT:
            raise ModeError("Platform Client mode committed; recovery must use S-C procedures.")
        try:
            clean = cleanup_and_verify() is True
        except BaseException:
            clean = False
        if clean:
            marker.unlink()
        raise
    else:
        # A successful commit is S-C; future S-C startup handles staged data.
        marker.unlink()
