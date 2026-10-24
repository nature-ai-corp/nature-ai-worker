"""Platform Client startup preflight. No local Agent or fallback to Standalone.

This does NOT establish Platform connectivity. Integration must consume the
existing S-C Client Interface contract and authenticate with the Platform.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .installation_mode import PLATFORM_CLIENT, ModeError, read_mode


@dataclass(frozen=True)
class PlatformPreflight:
    allowed: bool
    blockers: tuple[str, ...]


def check_platform_startup(data_dir: Path, *, sc_runtime_available: bool,
                           os_isolation_verified: bool,
                           credential_backend_verified: bool) -> PlatformPreflight:
    """Deny enterprise runtime if mode or mandatory security boundaries lack proof."""
    blockers: list[str] = []
    try:
        mode = read_mode(data_dir)
    except ModeError:
        blockers.append("invalid-mode-marker")
    else:
        if mode != PLATFORM_CLIENT:
            blockers.append("platform-client-installation-required")
    if not sc_runtime_available:
        blockers.append("s-c-interface-not-configured")
    if not os_isolation_verified:
        blockers.append("os-execution-isolation-unverified")
    if not credential_backend_verified:
        blockers.append("os-credential-backend-unverified")
    return PlatformPreflight(not blockers, tuple(blockers))


def require_platform_startup(data_dir: Path, *, sc_runtime_available: bool,
                             os_isolation_verified: bool,
                             credential_backend_verified: bool) -> None:
    result = check_platform_startup(
        data_dir,
        sc_runtime_available=sc_runtime_available,
        os_isolation_verified=os_isolation_verified,
        credential_backend_verified=credential_backend_verified,
    )
    if not result.allowed:
        raise ModeError("Platform Client startup blocked: " + ", ".join(result.blockers))
