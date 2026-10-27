# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Windows identities and NTFS permissions, read through native APIs (no writes).

The Windows Platform Client runs as its own virtual service account
(``NT SERVICE\\<service>``), whose SID is derived from the service name. Code is
admin-owned under Program Files; data is private to that account. This module
answers the questions the installer, the upgrader and the service preflight ask
about those boundaries. The analysis is plain Python and testable anywhere;
only reading a token or a DACL needs Windows.
"""
from __future__ import annotations

import hashlib
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"
TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
CREATOR_OWNER = "S-1-3-0"
# Accounts that already control the whole computer; access by them is not a leak.
MACHINE_ADMINS = frozenset({SYSTEM, ADMINISTRATORS, TRUSTED_INSTALLER})

ACCESS_ALLOWED = 0
INHERIT_ONLY = 0x08
# Rights that let a principal change a file or directory, its contents or its ACL.
WRITE_RIGHTS = (0x0002 | 0x0004 | 0x0010 | 0x0040 | 0x0100      # write/append data, EA, delete child, attrs
                | 0x00010000 | 0x00040000 | 0x00080000         # DELETE, WRITE_DAC, WRITE_OWNER
                | 0x10000000 | 0x40000000)                     # GENERIC_ALL, GENERIC_WRITE
# Rights on a parent that let a principal replace a child: delete child, or take over the ACL.
REPLACE_CHILD_RIGHTS = 0x0040 | 0x00040000 | 0x00080000 | 0x10000000


class WindowsSecurityError(RuntimeError):
    pass


def service_sid(name: str) -> str:
    """The SID of ``NT SERVICE\\<name>``: S-1-5-80 plus the SHA-1 of the upper-cased
    name in UTF-16LE, as five little-endian sub-authorities (documented by Microsoft)."""
    digest = hashlib.sha1(name.upper().encode("utf-16-le")).digest()
    return "S-1-5-80-" + "-".join(str(v) for v in struct.unpack("<5I", digest))


@dataclass(frozen=True)
class Ace:
    sid: str
    mask: int
    type: int = ACCESS_ALLOWED
    flags: int = 0

    @property
    def applies_here(self) -> bool:
        return not self.flags & INHERIT_ONLY


@dataclass(frozen=True)
class Acl:
    owner: str
    protected: bool          # does not inherit from the parent folder
    aces: tuple[Ace, ...]

    def grantees(self) -> set[str]:
        return {a.sid for a in self.aces if a.type == ACCESS_ALLOWED and a.applies_here and a.mask}

    def writers(self) -> set[str]:
        return {a.sid for a in self.aces if a.type == ACCESS_ALLOWED and a.applies_here
                and a.mask & WRITE_RIGHTS}

    def child_replacers(self) -> set[str]:
        return {a.sid for a in self.aces if a.type == ACCESS_ALLOWED and a.applies_here
                and a.mask & REPLACE_CHILD_RIGHTS}


def private_problems(acl: Acl, account_sid: str) -> list[str]:
    """Why a data root is not private to ``account_sid`` (empty: it is)."""
    problems = []
    if acl.owner not in MACHINE_ADMINS:
        problems.append(f"owner {acl.owner} is not SYSTEM or Administrators")
    if not acl.protected:
        problems.append("inherits permissions from its parent folder")
    others = acl.grantees() - MACHINE_ADMINS - {account_sid}
    if others:
        problems.append("also grants access to " + ", ".join(sorted(others)))
    if account_sid not in acl.grantees():
        problems.append(f"grants no access to {account_sid}")
    return problems


def admin_only_problems(acl: Acl) -> list[str]:
    """Why a code directory could be changed by someone other than an administrator."""
    problems = []
    if acl.owner not in MACHINE_ADMINS:
        problems.append(f"owner {acl.owner} is not SYSTEM, Administrators or TrustedInstaller")
    writers = acl.writers() - MACHINE_ADMINS
    if writers:
        problems.append("writable by " + ", ".join(sorted(writers)))
    return problems


def parent_problems(acl: Acl) -> list[str]:
    """Why a parent directory would let a non-administrator replace what it contains."""
    if acl.owner not in MACHINE_ADMINS:
        return [f"owner {acl.owner} is not an administrator"]
    replacers = acl.child_replacers() - MACHINE_ADMINS
    return ["lets " + ", ".join(sorted(replacers)) + " replace its contents"] if replacers else []


def system_tool(*parts: str) -> str:
    """Absolute path of a Windows system program. Elevated code never runs a tool by bare name:
    Windows would look in the calling interpreter's folder first, which may be user-writable."""
    root = r"C:\Windows\System32"
    import ctypes
    if sys.platform == "win32" and hasattr(ctypes, "windll"):
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.kernel32.GetSystemDirectoryW(buf, 260):
            root = buf.value
    return "\\".join((root, *parts))


# -- native reads (Windows only) ------------------------------------------------------

def _require_windows() -> None:
    if sys.platform != "win32":
        raise WindowsSecurityError("Requires Windows")


def _api():
    import ctypes
    from ctypes import wintypes
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return ctypes, wintypes, advapi32, kernel32


def _sid_string(sid_ptr) -> str:
    ctypes, wintypes, advapi32, kernel32 = _api()
    out = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(out)):
        raise WindowsSecurityError(f"ConvertSidToStringSid failed ({ctypes.get_last_error()})")
    try:
        return out.value
    finally:
        kernel32.LocalFree(out)


def read_acl(path: Path) -> Acl:
    """Owner, protection and access entries of a file or directory (links are not followed
    by callers: check ``is_symlink()`` first)."""
    _require_windows()
    ctypes, wintypes, advapi32, kernel32 = _api()
    owner, dacl, sd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    # SE_FILE_OBJECT = 1; OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION = 0x5
    err = advapi32.GetNamedSecurityInfoW(str(path), 1, 0x5, ctypes.byref(owner), None,
                                         ctypes.byref(dacl), None, ctypes.byref(sd))
    if err:
        raise WindowsSecurityError(f"Cannot read the permissions of {path} (error {err})")
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision)):
            raise WindowsSecurityError("GetSecurityDescriptorControl failed")
        aces = []
        if dacl.value:  # a NULL DACL grants everyone everything
            count = struct.unpack_from("<H", ctypes.string_at(dacl.value, 8), 4)[0]
            for i in range(count):
                ace = ctypes.c_void_p()
                if not advapi32.GetAce(dacl, i, ctypes.byref(ace)):
                    raise WindowsSecurityError("GetAce failed")
                ace_type, flags, _size, mask = struct.unpack("<BBHI", ctypes.string_at(ace.value, 8))
                if ace_type in (0, 1):  # ACCESS_ALLOWED_ACE / ACCESS_DENIED_ACE: SID follows the mask
                    aces.append(Ace(_sid_string(ace.value + 8), mask, ace_type, flags))
                else:
                    aces.append(Ace(f"unsupported-ace-type-{ace_type}", mask, ace_type, flags))
        else:
            aces.append(Ace("S-1-1-0", 0x10000000))
        return Acl(_sid_string(owner), bool(control.value & 0x1000), tuple(aces))
    finally:
        kernel32.LocalFree(sd)


FILE_ALL_ACCESS = 0x1F01FF
FILE_MODIFY = 0x1301BF


def protected_dacl_sddl(owner: str, grants: list[tuple[str, int]]) -> str:
    """Owner plus a protected DACL holding exactly ``grants``, inherited by everything inside."""
    for sid in (owner, *(g[0] for g in grants)):
        if not sid.startswith("S-1-") or not all(part.isdigit() for part in sid[4:].split("-")):
            raise WindowsSecurityError(f"Invalid SID {sid!r}")
    return f"O:{owner}D:PAI" + "".join(f"(A;OICI;0x{mask:x};;;{sid})" for sid, mask in grants)


def set_protected_dacl(path: Path, owner: str, grants: list[tuple[str, int]]) -> None:
    """Replace the owner and the whole DACL of ``path`` in one call (SetNamedSecurityInfoW).
    Every other entry, explicit or inherited, is gone afterwards; nothing is looked up by name."""
    _require_windows()
    ctypes, wintypes, advapi32, kernel32 = _api()
    sd = ctypes.c_void_p()
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            protected_dacl_sddl(owner, grants), 1, ctypes.byref(sd), None):
        raise WindowsSecurityError(f"Invalid security descriptor ({ctypes.get_last_error()})")
    try:
        owner_sid, dacl = ctypes.c_void_p(), ctypes.c_void_p()
        defaulted, present = wintypes.BOOL(), wintypes.BOOL()
        advapi32.GetSecurityDescriptorOwner.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                                        ctypes.POINTER(wintypes.BOOL)]
        advapi32.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                                       ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
        if not (advapi32.GetSecurityDescriptorOwner(sd, ctypes.byref(owner_sid), ctypes.byref(defaulted)) and
                advapi32.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl),
                                                   ctypes.byref(defaulted))):
            raise WindowsSecurityError(f"Cannot read the new security descriptor ({ctypes.get_last_error()})")
        advapi32.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
                                                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
        # SE_FILE_OBJECT; OWNER | DACL | PROTECTED_DACL_SECURITY_INFORMATION
        err = advapi32.SetNamedSecurityInfoW(str(path), 1, 0x1 | 0x4 | 0x80000000, owner_sid, None, dacl, None)
        if err:
            raise WindowsSecurityError(f"Cannot set the permissions of {path} (error {err})")
    finally:
        kernel32.LocalFree(sd)


def lookup_account_sid(name: str) -> str:
    """The SID Windows itself maps ``name`` to (LookupAccountNameW). ``NT SERVICE\\<service>``
    resolves only while that service is registered with the Service Control Manager."""
    _require_windows()
    ctypes, wintypes, advapi32, kernel32 = _api()
    advapi32.LookupAccountNameW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_int)]
    sid_size, domain_size, use = wintypes.DWORD(0), wintypes.DWORD(0), ctypes.c_int()
    advapi32.LookupAccountNameW(None, name, None, ctypes.byref(sid_size), None,
                                ctypes.byref(domain_size), ctypes.byref(use))
    error = ctypes.get_last_error()
    if error != 122:  # ERROR_INSUFFICIENT_BUFFER: the account exists, sizes are known
        raise WindowsSecurityError(f"Windows cannot resolve the account {name} (error {error})")
    sid = ctypes.create_string_buffer(sid_size.value)
    domain = ctypes.create_unicode_buffer(domain_size.value)
    if not advapi32.LookupAccountNameW(None, name, sid, ctypes.byref(sid_size), domain,
                                       ctypes.byref(domain_size), ctypes.byref(use)):
        raise WindowsSecurityError(f"Windows cannot resolve the account {name} ({ctypes.get_last_error()})")
    return _sid_string(ctypes.addressof(sid))


@dataclass(frozen=True)
class Token:
    user: str
    groups: frozenset[str]   # enabled groups only (deny-only groups, e.g. under UAC, excluded)
    elevated: bool

    @property
    def administrator(self) -> bool:
        return ADMINISTRATORS in self.groups or self.user == SYSTEM


def current_token() -> Token:
    _require_windows()
    ctypes, wintypes, advapi32, kernel32 = _api()
    handle = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(handle)):
        raise WindowsSecurityError(f"OpenProcessToken failed ({ctypes.get_last_error()})")

    def info(cls: int) -> ctypes.Array:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(handle, cls, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(handle, cls, buf, size, ctypes.byref(size)):
            raise WindowsSecurityError(f"GetTokenInformation({cls}) failed ({ctypes.get_last_error()})")
        return buf

    ptr = ctypes.sizeof(ctypes.c_void_p)
    try:
        user_buf = info(1)                                   # TokenUser: SID_AND_ATTRIBUTES
        user = _sid_string(ctypes.c_void_p.from_buffer(user_buf).value)
        groups_buf = info(2)                                 # TokenGroups: count, then entries
        count = ctypes.c_uint32.from_buffer(groups_buf).value
        groups = set()
        for i in range(count):
            offset = ptr + i * 2 * ptr                       # DWORD count padded to pointer size
            sid = ctypes.c_void_p.from_buffer(groups_buf, offset).value
            attributes = ctypes.c_uint32.from_buffer(groups_buf, offset + ptr).value
            if attributes & 0x4 and not attributes & 0x10:  # SE_GROUP_ENABLED, not USE_FOR_DENY_ONLY
                groups.add(_sid_string(sid))
        elevated = bool(ctypes.c_uint32.from_buffer(info(20)).value)  # TokenElevation
        return Token(user, frozenset(groups), elevated)
    finally:
        kernel32.CloseHandle(handle)
