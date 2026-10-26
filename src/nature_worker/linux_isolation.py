"""Linux Platform Client execution-boundary probe (read-only).

The Platform Client must run as its own unprivileged OS account, separate
from the account that runs the Standalone Agent, with a data root only that
account can enter. Separate paths under one account are NOT isolation. Every
finding is evidence for the preflight; an unknown is never treated as clean.

Not covered here, and stated so rather than assumed: root and anyone who can
become root (including WSL's Windows-side ``wsl -u root``) can read any
process or file.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from .os_probes import ProbeFinding

PRIVILEGED_GROUPS = frozenset({"sudo", "wheel", "admin", "root", "docker", "lxd"})


def _group_names(gids) -> set[str]:
    import grp  # POSIX only; this module must stay importable on Windows
    names = set()
    for gid in gids:
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    return names


def inspect_linux_isolation(sc_root: Path, standalone_root: Path,
                            standalone_uid: int | None = None) -> tuple[ProbeFinding, ...]:
    """``standalone_uid``: the Standalone owner, recorded by the root installer
    because the Platform Client account cannot (and must not) enter that home."""
    if not sys.platform.startswith("linux"):
        return (ProbeFinding("linux_isolation", None, "Requires Linux"),)
    findings: list[ProbeFinding] = []
    uid = os.geteuid()
    findings.append(ProbeFinding("unprivileged_account", uid != 0,
                                 "Platform Client runs as root" if uid == 0 else f"uid {uid}"))
    privileged = _group_names(os.getgroups()) & PRIVILEGED_GROUPS
    findings.append(ProbeFinding(
        "no_privileged_groups", not privileged,
        "member of " + ", ".join(sorted(privileged)) if privileged else "none"))

    try:
        info = os.lstat(sc_root)
    except OSError:
        findings.append(ProbeFinding("sc_root_private", None, "S-C data root not provisioned"))
    else:
        private = (stat.S_ISDIR(info.st_mode) and info.st_uid == uid
                   and stat.S_IMODE(info.st_mode) & 0o077 == 0)
        findings.append(ProbeFinding(
            "sc_root_private", private,
            "owned by this account, mode 0700" if private else
            "S-C data root must be a directory owned by this account with no group/other access"))

    try:
        owner = standalone_uid if standalone_uid is not None else os.stat(standalone_root).st_uid
    except FileNotFoundError:
        findings.append(ProbeFinding("separate_from_standalone", True, "no Standalone data root"))
    except OSError:
        findings.append(ProbeFinding("separate_from_standalone", None, "cannot inspect Standalone root"))
    else:
        findings.append(ProbeFinding(
            "separate_from_standalone", owner != uid and owner != 0,
            "Standalone runs under a different account" if owner != uid and owner != 0 else
            "Platform Client must not share the Standalone account"))
    resolved_sc, resolved_sa = sc_root.resolve(), standalone_root.resolve()
    overlap = (resolved_sc == resolved_sa or resolved_sa in resolved_sc.parents
               or resolved_sc in resolved_sa.parents)
    findings.append(ProbeFinding("independent_data_roots", not overlap,
                                 "overlapping data roots" if overlap else "independent"))
    return tuple(findings)


def linux_isolation_verified(findings: tuple[ProbeFinding, ...]) -> bool:
    return bool(findings) and all(f.clean is True for f in findings)
