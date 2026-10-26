# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Local Platform Client path inventory and read-only clean-install checks.

This module deliberately does NOT create S-C storage, decrypt enterprise data,
delete user files, or claim OS-level isolation. Installers can use the report
after uninstall/authorized cleanup; enforcement must also be at installer and
enterprise device-management boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SC_SUBDIRECTORIES = (
    "credentials", "sessions", "cache", "files", "indexes", "logs", "tmp", "state"
)


@dataclass(frozen=True)
class CleanReport:
    clean: bool
    residues: tuple[str, ...]


def platform_paths(root: Path) -> dict[str, Path]:
    """Return all dedicated S-C locations without making any directories."""
    return {name: root / name for name in SC_SUBDIRECTORIES}


def validate_clean(sc_root: Path, *, active_processes: bool = False,
                   credential_entries: tuple[str, ...] = (),
                   installation_markers: tuple[Path, ...] = ()) -> CleanReport:
    """Fail closed on any S-C residue known to this validator.

    This is a *partial* validator: callers must separately probe OS credential
    stores, running processes, installed app records and additional OS-specific
    locations. The caller is responsible for a trustworthy S-C root.
    A symlink is residue; this function NEVER follows it or deletes anything.
    """
    residues: list[str] = []
    if sc_root.is_symlink():
        residues.append(f"symlink:{sc_root}")
    elif sc_root.exists():
        # An existing enterprise root itself is residue, even when empty:
        # do not silently bless an abandoned installation.
        residues.append(f"root:{sc_root}")
    for label, path in platform_paths(sc_root).items():
        if path.is_symlink() or path.exists():
            residues.append(f"{label}:{path}")
    for marker in installation_markers:
        if marker.is_symlink() or marker.exists():
            residues.append(f"marker:{marker}")
    if active_processes:
        residues.append("running-platform-client")
    for credential in credential_entries:
        residues.append(f"os-credential:{credential}")
    return CleanReport(not residues, tuple(residues))
