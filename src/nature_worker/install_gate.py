"""Installer-facing clean-install decision.

A filesystem-only check is never enough to authorize an S-C to Standalone
reinstall. Each OS-level probe must report verified clean independently.
No probe may be silently skipped or treated as clean by default.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .platform_storage import CleanReport, validate_clean

_REQUIRED = frozenset({
    "os_credentials", "running_processes", "installed_packages",
    "other_app_locations", "device_policy",
})


@dataclass(frozen=True)
class CleanEvidence:
    checks: dict[str, bool | None]

    def __post_init__(self) -> None:
        if set(self.checks) != _REQUIRED:
            raise ValueError("All mandatory OS cleanup probes must be present.")
        if any(value is not None and not isinstance(value, bool)
               for value in self.checks.values()):
            raise ValueError("Probe results must be true, false or unknown.")


@dataclass(frozen=True)
class InstallDecision:
    allowed: bool
    blockers: tuple[str, ...]


def standalone_install_decision(sc_root: Path, evidence: CleanEvidence,
                                *, mode_marker: Path) -> InstallDecision:
    """Allow only after S-C residue absence AND verified OS probe results.

    `True` indicates verified clean. `None` is unsupported/unknown and blocks.
    The mode marker from a Platform Client install must be absent, not simply
    rewritten to standalone. This function is advisory until wired to the
    actual Windows/Linux installer with trusted OS probes.
    """
    local: CleanReport = validate_clean(sc_root, installation_markers=(mode_marker,))
    blockers = list(local.residues)
    for check, status in evidence.checks.items():
        if status is not True:
            blockers.append(f"unverified-or-unclean:{check}")
    return InstallDecision(allowed=not blockers, blockers=tuple(blockers))
