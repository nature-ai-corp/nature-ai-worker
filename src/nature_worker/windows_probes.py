"""Read-only Windows S-C residue hints.

Windows Registry uninstall entries are not an exhaustive installation inventory,
and absence never permits a fresh Standalone installation. No registry writes.
"""
from __future__ import annotations

from .os_probes import ProbeFinding


def inspect_windows_uninstall_registry() -> ProbeFinding:
    try:
        import winreg
    except ImportError:
        return ProbeFinding("installed_packages", None, "Windows registry unavailable")

    bases = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    searched = 0
    try:
        for hive in hives:
            for base in bases:
                try:
                    key = winreg.OpenKey(hive, base, 0, winreg.KEY_READ)
                except FileNotFoundError:
                    continue
                with key:
                    index = 0
                    while True:
                        try:
                            child = winreg.EnumKey(key, index)
                        except OSError as exc:
                            if getattr(exc, "winerror", None) == 259:
                                break
                            raise
                        index += 1
                        with winreg.OpenKey(key, child, 0, winreg.KEY_READ) as entry:
                            try:
                                name = str(winreg.QueryValueEx(entry, "DisplayName")[0]).lower()
                            except FileNotFoundError:
                                continue
                            searched += 1
                            if "nature ai worker" in name and ("platform" in name or "s-c" in name):
                                return ProbeFinding("installed_packages", False,
                                                    "S-C related uninstall registration found")
    except (OSError, PermissionError):
        return ProbeFinding("installed_packages", None,
                            "Could not fully enumerate Windows uninstall registry")
    return ProbeFinding("installed_packages", None,
                        f"Inspected {searched} uninstall display names; absence is not proof of clean install")
