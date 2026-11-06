# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Which installed build a process runs, and whether it is the expected one.

The application version (``__version__``, e.g. ``0.1.0``) is not the installed
build: a local build is installed as ``versions/0.1.0+g<commit>``, a signed
release as ``versions/<version>``. Health checks after install, upgrade and
rollback compare the build the answering process was loaded from with the
build that was activated, exactly. Standard library only (the Windows
uninstaller runs from a copy without dependencies).
"""
from __future__ import annotations

import re
from pathlib import Path

_BUILD = re.compile(r"^[0-9][0-9.]*[+a-z0-9.]*$")


def running_build(module_file: str | None = None) -> str | None:
    """The ``versions/<build>`` directory this code was imported from, or None when it does not
    run from a managed installation (a source checkout, a plain ``pip install``)."""
    here = Path(module_file or __file__).resolve()
    for directory in here.parents:
        versions = directory.parent
        if versions.name == "versions" and (versions.parent / "installation.json").is_file():
            return directory.name if _BUILD.match(directory.name) else None
    return None


def build_mismatch(status: dict, expected: str) -> str | None:
    """None when the process that answered runs exactly ``expected``; otherwise why not.

    Builds from before the ``build`` field existed are identified by their version, which is
    only exact for signed releases (whose build is their version)."""
    if "build" in status:
        build = status.get("build")
        return None if build == expected else f"it runs build {build!r}, not {expected}"
    version = status.get("version")
    return None if version == expected else f"it reports version {version!r} and no build, not {expected}"
