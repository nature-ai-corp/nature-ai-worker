"""Best-effort, read-only host probes for installer diagnostics.

These probes are deliberately NOT authority to approve Standalone installation.
Some OS credential vault entries, package installations and enterprise device
policies cannot be exhaustively enumerated from an unprivileged application.
Unknown is an explicit result, never a clean success.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProbeFinding:
    check: str
    clean: bool | None  # True verified, False residue, None unknown
    detail: str


def inspect_processes_linux(proc: Path = Path("/proc")) -> ProbeFinding:
    """Inspect visible Linux command lines for a running Platform Client.

    Even when none are visible, other users' processes may be inaccessible.
    Hence negative results are UNKNOWN, not verified clean.
    """
    if not proc.is_dir():
        return ProbeFinding("running_processes", None, "Linux procfs unavailable")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").lower()
        except (OSError, PermissionError):
            continue
        if b"nature-worker-platform-client" in cmd or b"nature_worker.platform_client" in cmd:
            return ProbeFinding("running_processes", False, f"Potential S-C process PID {entry.name}")
    return ProbeFinding("running_processes", None, "No visible S-C process; full process inventory not verified")


def inspect_known_local_paths(paths: tuple[Path, ...]) -> ProbeFinding:
    """Detect path residues without following symlinks or modifying anything."""
    for path in paths:
        try:
            if path.is_symlink() or path.exists():
                return ProbeFinding("other_app_locations", False, f"Residue: {path}")
        except OSError:
            return ProbeFinding("other_app_locations", None, f"Cannot inspect: {path}")
    return ProbeFinding("other_app_locations", None,
                        "No residue in supplied paths; other OS paths not yet verified")


def local_os_diagnostics(paths: tuple[Path, ...] = ()) -> tuple[ProbeFinding, ...]:
    process = (inspect_processes_linux() if sys.platform.startswith("linux")
               else ProbeFinding("running_processes", None, "OS-specific process probe unavailable"))
    return (
        process,
        inspect_known_local_paths(paths),
        ProbeFinding("os_credentials", None, "OS vault enumeration not implemented"),
        ProbeFinding("installed_packages", None, "Installer inventory not verified"),
        ProbeFinding("device_policy", None, "Enterprise device policy cannot be verified locally"),
    )
