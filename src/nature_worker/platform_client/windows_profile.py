# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""The Windows user profile of the Platform Client's virtual service account.

Windows creates %SystemRoot%\\ServiceProfiles\\<service> (registered in ProfileList under the
service SID) the first time the service runs, and keeps it when the service is deleted. The
Platform Client stores nothing there itself: its data root is its only storage, and it uses no
Credential Manager and no DPAPI. What Windows or a library puts there is still the service
account's state, so it is inspected by name and size only (contents are never read) and:

- an ordinary uninstall keeps it, and says so;
- ``uninstall --remove-service-profile`` removes it with Windows' own DeleteProfileW, only when it
  is verifiably this service's profile and holds nothing meaningful (``Profile.problems``).

Nothing here deletes files itself, and no folder other than the one ProfileList names for this
service's SID is ever considered.
"""
from __future__ import annotations

import ntpath
import os
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from ..windows_security import ADMINISTRATORS, SYSTEM, WindowsSecurityError

PROFILE_LIST = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
# What Windows itself creates in a service profile (relative paths, compared without case): the
# registry hive and its logs, and the AppData tree. Anything else (Desktop, Documents, ...) is
# treated as data someone made, and blocks removal.
_WINDOWS_ITEMS = ("ntuser.dat", "ntuser.ini", "ntuser.pol", "appdata")
_CREDENTIALS = ("appdata/local/microsoft/credentials", "appdata/roaming/microsoft/credentials")
_DPAPI_KEYS = "appdata/roaming/microsoft/protect"
_MANIFEST_LIMIT = 200


def profiles_root() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "ServiceProfiles"


@dataclass
class Profile:
    service: str
    sid: str
    expected: Path                       # %SystemRoot%\ServiceProfiles\<service>
    registered: str | None = None        # ProfileImagePath of the service SID, as Windows wrote it
    exists: bool = False
    owner: str | None = None
    loaded: bool | None = None           # its registry hive is loaded (HKEY_USERS\<sid>)
    unreadable: bool = False             # its permissions or part of it could not be read
    credential_files: list[str] = field(default_factory=list)
    dpapi_keys: list[str] = field(default_factory=list)
    other_data: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)   # "relative\\path (size bytes)"
    problems: list[str] = field(default_factory=list)  # reasons it must not be removed

    @property
    def present(self) -> bool:
        return self.exists or self.registered is not None

    def summary(self) -> dict:
        return {"path": str(self.expected), "registered_path": self.registered, "exists": self.exists,
                "owner": self.owner, "loaded": self.loaded, "unreadable": self.unreadable, "credential_files": self.credential_files,
                "dpapi_master_keys": self.dpapi_keys, "other_data": self.other_data,
                "file_count": len(self.files), "removable": self.present and not self.problems,
                "problems": self.problems}


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix().lower()


def inspect(system, service: str, sid: str, *, root: Path | None = None, service_registered: bool,
            data_kept: bool) -> Profile:
    """Identity, ownership and contents (names and sizes) of the service's profile."""
    profile = Profile(service, sid, (root or profiles_root()) / service)
    registered = system.get_registry(rf"{PROFILE_LIST}\{sid}", "ProfileImagePath")
    profile.registered = str(registered) if registered else None
    profile.exists = profile.expected.exists() or system.is_link(profile.expected)
    if not profile.present:
        return profile
    problems = profile.problems
    if service_registered:
        problems.append(f"service {service} is still registered")
    if profile.registered is None:
        problems.append(f"ProfileList has no entry for {sid}: Windows did not create {profile.expected} "
                        "for this service account")
    elif _norm(ntpath.expandvars(profile.registered)) != _norm(str(profile.expected)):
        problems.append(f"ProfileList names {profile.registered} for {sid}, not {profile.expected}")
    if not profile.exists:
        problems.append(f"{profile.expected} does not exist (ProfileList entry only)")
        return profile
    if system.is_link(profile.expected):
        problems.append(f"{profile.expected} is a link or junction")
        return profile
    try:
        acl = system.read_acl(profile.expected)
    except (OSError, WindowsSecurityError) as exc:
        profile.unreadable = True
        problems.append(f"its permissions cannot be read ({exc})")
        return profile
    profile.owner = acl.owner
    if acl.owner not in (sid, SYSTEM, ADMINISTRATORS):
        problems.append(f"owned by {acl.owner}, not by the service account, SYSTEM or Administrators")
    if sid not in acl.grantees():
        problems.append("its permissions do not grant the service account: not a profile Windows made for it")
    profile.loaded = system.user_hive_loaded(sid)
    if profile.loaded:
        problems.append("Windows has its registry hive loaded (the account is still signed in)")
    _walk(system, profile)
    if profile.credential_files:
        problems.append(f"it holds {len(profile.credential_files)} Credential Manager file(s) of the service "
                        "account; credentials are never removed automatically")
    if profile.dpapi_keys and data_kept:
        problems.append("it holds DPAPI master keys and the data root is kept: anything DPAPI-protected "
                        "by the service account would become unreadable")
    if profile.other_data:
        problems.append(f"it holds data outside what Windows creates: {', '.join(profile.other_data[:10])}")
    return profile


def _norm(path: str) -> str:
    return str(PureWindowsPath(path)).rstrip("\\").lower()


def _walk(system, profile: Profile) -> None:
    """Every file by name and size, without following links and without reading contents."""
    root = profile.expected

    def unlisted(exc: OSError) -> None:
        profile.unreadable = True
        profile.problems.append(f"{exc.filename} cannot be listed ({exc.strerror or exc})")

    for directory, dirs, names in os.walk(root, onerror=unlisted):
        here = Path(directory)
        for name in list(dirs) + list(names):
            if system.is_link(here / name):
                # Windows' compatibility junctions ("Application Data", ...) or anything else:
                # listed, never followed (DeleteProfileW does not follow them either).
                (dirs if name in dirs else names).remove(name)
                profile.files.append(f"{(here / name).relative_to(root)} (link, not followed)")
        for name in names + dirs:
            path = here / name
            rel = _rel(path, root)
            if here == root and rel not in _WINDOWS_ITEMS and not rel.startswith("ntuser.dat"):
                profile.other_data.append(name)
            if name in dirs:
                continue
            if any(rel.startswith(c + "/") for c in _CREDENTIALS):
                profile.credential_files.append(str(path.relative_to(root)))
            elif rel.startswith(_DPAPI_KEYS + "/"):
                profile.dpapi_keys.append(str(path.relative_to(root)))
            try:
                size = os.lstat(path).st_size
            except OSError:
                size = "?"
            if len(profile.files) < _MANIFEST_LIMIT:
                profile.files.append(f"{path.relative_to(root)} ({size} bytes)")
            elif len(profile.files) == _MANIFEST_LIMIT:
                profile.files.append("...")


def remove(system, profile: Profile) -> str | None:
    """DeleteProfileW for a profile ``inspect`` found removable; None, or what is still there."""
    if not profile.present or profile.problems:
        raise ValueError("only a profile inspect() found removable can be removed")
    system.delete_profile(profile.sid)
    left = []
    if system.get_registry(rf"{PROFILE_LIST}\{profile.sid}", "ProfileImagePath") is not None:
        left.append(f"HKLM\\{PROFILE_LIST}\\{profile.sid}")
    if profile.expected.exists():
        left.append(str(profile.expected))
    return ", ".join(left) or None
