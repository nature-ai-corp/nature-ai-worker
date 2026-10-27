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

import fnmatch
import ntpath
import os
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from ..windows_security import ADMINISTRATORS, SYSTEM, WindowsSecurityError

PROFILE_LIST = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
# What Windows creates in a service profile (from the Default profile and the account's registry
# hives), as observed natively in NatureWorkerPlatformClientE2E's profile (8fa4a0f, 86 entries).
# Relative paths, compared without case; fnmatch's "*" also matches "\". Folders are not data:
# only files and links are classified, so the standard empty folders (Desktop, Documents, ...)
# are fine, but any file in them other than desktop.ini is not.
_STANDARD_FOLDERS = ("desktop", "documents", "downloads", "favorites", "links", "music", "onedrive",
                     "pictures", "saved games", "videos", "contacts", "searches", "3d objects")
_WINDOWS_FILES = (
    "ntuser.dat*", "ntuser.ini", "ntuser.pol",                          # the user hive, its logs
    "appdata/local/microsoft/windows/usrclass.dat*",                    # the classes hive, its logs
    "appdata/local/microsoft/windows/shell/defaultlayouts.xml",
    "appdata/local/microsoft/windows/winx/group*/*.lnk", "appdata/local/microsoft/windows/winx/group*/desktop.ini",
    "appdata/local/microsoft/windows sidebar/settings.ini",
    "appdata/roaming/microsoft/internet explorer/quick launch/*.lnk",
    "appdata/roaming/microsoft/internet explorer/quick launch/desktop.ini",
    "appdata/roaming/microsoft/windows/sendto/*.lnk", "appdata/roaming/microsoft/windows/sendto/*.zfsendtotarget",
    "appdata/roaming/microsoft/windows/sendto/*.desklink", "appdata/roaming/microsoft/windows/sendto/*.mapimail",
    "appdata/roaming/microsoft/windows/sendto/desktop.ini",
    "appdata/roaming/microsoft/windows/start menu/programs/*.lnk",
    "appdata/roaming/microsoft/windows/start menu/programs/*desktop.ini",
    *(f"{folder}/desktop.ini" for folder in _STANDARD_FOLDERS),
)
# Windows' compatibility junctions (for programs written for Windows XP) and the profile folder
# each must point to. Any other link, or one of these pointing anywhere else, blocks removal.
_WINDOWS_LINKS = {
    "application data": "appdata/roaming", "cookies": "appdata/local/microsoft/windows/inetcookies",
    "local settings": "appdata/local", "my documents": "documents",
    "nethood": "appdata/roaming/microsoft/windows/network shortcuts",
    "printhood": "appdata/roaming/microsoft/windows/printer shortcuts",
    "recent": "appdata/roaming/microsoft/windows/recent", "sendto": "appdata/roaming/microsoft/windows/sendto",
    "start menu": "appdata/roaming/microsoft/windows/start menu",
    "templates": "appdata/roaming/microsoft/windows/templates",
    "appdata/local/application data": "appdata/local",
    "appdata/local/history": "appdata/local/microsoft/windows/history",
    "appdata/local/temporary internet files": "appdata/local/microsoft/windows/inetcache",
    "appdata/local/microsoft/windows/temporary internet files": "appdata/local/microsoft/windows/inetcache",
    "documents/my music": "music", "documents/my pictures": "pictures", "documents/my videos": "videos",
}
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
    other_data: list[str] = field(default_factory=list)   # files Windows does not create there
    bad_links: list[str] = field(default_factory=list)    # links other than Windows' own junctions
    files: list[str] = field(default_factory=list)   # "relative\\path (size bytes)"
    problems: list[str] = field(default_factory=list)  # reasons it must not be removed

    @property
    def present(self) -> bool:
        return self.exists or self.registered is not None

    def summary(self) -> dict:
        return {"path": str(self.expected), "registered_path": self.registered, "exists": self.exists,
                "owner": self.owner, "loaded": self.loaded, "unreadable": self.unreadable, "credential_files": self.credential_files,
                "dpapi_master_keys": self.dpapi_keys, "other_data": self.other_data, "bad_links": self.bad_links,
                "file_count": len(self.files), "removable": self.present and not self.problems,
                "problems": self.problems}


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix().lower()


def inspect(system, service: str, sid: str, *, root: Path | None = None, service_registered: bool,
            data_kept: bool) -> Profile:
    """Identity, ownership and contents (names and sizes) of the service's profile."""
    profile = Profile(service, sid, (root or profiles_root()) / service)
    problems = profile.problems
    try:  # an inspection never fails an uninstall: what cannot be read is a reason to keep it
        registered = system.get_registry(rf"{PROFILE_LIST}\{sid}", "ProfileImagePath")
        profile.exists = profile.expected.exists() or system.is_link(profile.expected)
    except OSError as exc:
        profile.unreadable, profile.exists = True, True
        problems.append(f"it cannot be inspected ({exc})")
        return profile
    profile.registered = str(registered) if registered else None
    if not profile.present:
        return profile
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
    try:
        profile.loaded = system.user_hive_loaded(sid)
    except OSError as exc:
        problems.append(f"whether its registry hive is loaded cannot be read ({exc})")
    if profile.loaded:
        # Seen natively after the service was deleted: Windows unloads the hive on a restart.
        problems.append("Windows has its registry hive loaded (restart Windows, then try again)")
    _walk(system, profile)
    if profile.credential_files:
        problems.append(f"it holds {len(profile.credential_files)} Credential Manager file(s) of the service "
                        "account; credentials are never removed automatically")
    if profile.dpapi_keys and data_kept:
        problems.append("it holds DPAPI master keys and the data root is kept: anything DPAPI-protected "
                        "by the service account would become unreadable")
    if profile.bad_links:
        problems.append(f"it holds links that are not Windows' own junctions: {', '.join(profile.bad_links[:10])}")
    if profile.other_data:
        problems.append(f"it holds files Windows does not create: {', '.join(profile.other_data[:10])}")
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
                # Listed, never followed; only Windows' own junctions to their own folder pass.
                (dirs if name in dirs else names).remove(name)
                path = here / name
                profile.files.append(f"{path.relative_to(root)} (link, not followed)")
                if not _windows_link(path, root):
                    profile.bad_links.append(str(path.relative_to(root)))
        for name in names:
            path = here / name
            rel = _rel(path, root)
            if any(rel.startswith(c + "/") for c in _CREDENTIALS):
                profile.credential_files.append(str(path.relative_to(root)))
            elif rel.startswith(_DPAPI_KEYS + "/"):
                profile.dpapi_keys.append(str(path.relative_to(root)))
            elif not any(fnmatch.fnmatchcase(rel, pattern) for pattern in _WINDOWS_FILES):
                profile.other_data.append(str(path.relative_to(root)))
            try:
                size = os.lstat(path).st_size
            except OSError:
                size = "?"
            if len(profile.files) < _MANIFEST_LIMIT:
                profile.files.append(f"{path.relative_to(root)} ({size} bytes)")
            elif len(profile.files) == _MANIFEST_LIMIT:
                profile.files.append("...")


def _windows_link(path: Path, root: Path) -> bool:
    """One of Windows' compatibility junctions, pointing to its own folder in this profile."""
    expected = _WINDOWS_LINKS.get(_rel(path, root))
    if expected is None:
        return False
    try:
        target = os.readlink(path)
    except OSError:
        return False
    if target.startswith("\\??\\") or target.startswith("\\\\?\\"):  # NT and Win32 long-path prefixes
        target = target[4:]
    return _norm(target) == _norm(str(root) + "\\" + expected.replace("/", "\\"))


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
