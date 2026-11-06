# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Installer orchestration for a guarded Standalone to Platform Client transition.

Trusted adapters supply real Platform login, S-C contract and OS checks.
This module does not invent endpoints or authorize an unverified install.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .installation_mode import ModeError
from .mode_transition import commit_platform_transition
from .transition_cleanup import attempt_platform_transition


@dataclass(frozen=True)
class TransitionOperations:
    authenticate_platform: Callable[[], bool]
    verify_sc_interface: Callable[[], bool]
    verify_os_isolation: Callable[[], bool]
    verify_credential_backend: Callable[[], bool]
    verify_staging_ready: Callable[[], bool]
    cleanup_staging_and_verify: Callable[[], bool]


def switch_to_platform_client(data_dir: Path, operations: TransitionOperations) -> None:
    """Caller must hold an exclusive installer lock; never stage into Standalone."""
    def prepare() -> bool:
        for step in (operations.verify_os_isolation,
                     operations.verify_credential_backend,
                     operations.authenticate_platform,
                     operations.verify_sc_interface,
                     operations.verify_staging_ready):
            if step() is not True:
                raise ModeError("S-C preparation failed.")
        return True

    attempt_platform_transition(
        data_dir, prepare=prepare,
        cleanup_and_verify=operations.cleanup_staging_and_verify,
        commit=lambda: commit_platform_transition(data_dir, verify_ready=lambda: True),
    )
