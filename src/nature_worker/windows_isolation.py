# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Windows Platform Client execution-boundary probe (read-only).

The Platform Client must run as its own Windows identity (the installer uses
the virtual account ``NT SERVICE\\NatureWorkerPlatformClient``), not as the user
who runs the Standalone Agent, and must not hold administrator rights. Its
data root must be private to that identity: protected from inheritance and
granting nothing to Users, Authenticated Users, Everyone or the Standalone
user. Separate folders under one Windows user are NOT isolation. An unknown
is never treated as clean.

Not covered here, and stated so rather than assumed: administrators (and an
administrator user who approves a UAC prompt) can read any file or process.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .os_probes import ProbeFinding
from .windows_security import (
    SYSTEM, Token, WindowsSecurityError, current_token, private_problems, read_acl,
)


def evaluate_windows_isolation(token: Token, sc_root: Path, standalone_root: Path,
                               standalone_sid: str | None, read=read_acl) -> tuple[ProbeFinding, ...]:
    findings: list[ProbeFinding] = []
    privileged = token.administrator or token.elevated or token.user == SYSTEM
    findings.append(ProbeFinding(
        "unprivileged_account", not privileged,
        "runs with administrator or SYSTEM rights" if privileged else token.user))

    if sc_root.is_symlink():
        findings.append(ProbeFinding("sc_root_private", False, "S-C data root is a link"))
    elif not sc_root.is_dir():
        findings.append(ProbeFinding("sc_root_private", None, "S-C data root not provisioned"))
    else:
        try:
            problems = private_problems(read(sc_root), token.user)
        except WindowsSecurityError as exc:
            findings.append(ProbeFinding("sc_root_private", None, str(exc)))
        else:
            findings.append(ProbeFinding(
                "sc_root_private", not problems,
                "private to this account (SYSTEM and Administrators aside)" if not problems
                else "; ".join(problems)))

    if not standalone_sid:
        findings.append(ProbeFinding("separate_from_standalone", None,
                                     "the Standalone user's SID was not recorded by the installer"))
    else:
        separate = standalone_sid != token.user
        findings.append(ProbeFinding(
            "separate_from_standalone", separate,
            "Standalone runs under a different Windows account" if separate else
            "Platform Client must not run as the Standalone user"))

    resolved_sc, resolved_sa = sc_root.resolve(), standalone_root.resolve()
    overlap = (resolved_sc == resolved_sa or resolved_sa in resolved_sc.parents
               or resolved_sc in resolved_sa.parents)
    findings.append(ProbeFinding("independent_data_roots", not overlap,
                                 "overlapping data roots" if overlap else "independent"))
    return tuple(findings)


def inspect_windows_isolation(sc_root: Path, standalone_root: Path,
                              standalone_sid: str | None) -> tuple[ProbeFinding, ...]:
    if sys.platform != "win32":
        return (ProbeFinding("windows_isolation", None, "Requires Windows"),)
    try:
        token = current_token()
    except WindowsSecurityError as exc:
        return (ProbeFinding("windows_isolation", None, str(exc)),)
    return evaluate_windows_isolation(token, sc_root, standalone_root, standalone_sid)
