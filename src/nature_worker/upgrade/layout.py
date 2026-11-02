# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""A managed installation on disk, shared by Standalone and Platform Client::

    <prefix>/
      installation.json    edition, data/workspace roots, service identity (fixed at install)
      current              the active version, one line, replaced atomically
      trusted-keys.json    optional locally trusted release keys (never "official")
      versions/<version>/  one complete installation per version, never modified once complete:
          venv/  INSTALLED.json  RELEASE_NOTES.md  [.incomplete while being built]
      bin/                 launchers that start versions/<current>
      upgrade/             upgrade.lock, state.json (journal), history.jsonl, staging/, logs/

Switching versions is a single atomic replace of ``current``: there is never a
mix of two versions' code, and a running process keeps its own version's files.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ..maintenance import RuntimeLock, read_text, replace_file
from .release import EDITIONS, ReleaseError, Version

INSTALLATION = "installation.json"
CURRENT = "current"
LOCAL_KEYS = "trusted-keys.json"
INCOMPLETE = ".incomplete"
INSTALLED = "INSTALLED.json"
PREFIX_ENV = "NATURE_WORKER_INSTALL_PREFIX"
# AppId of the Windows Standalone installer (packaging/windows/NatureAIWorker.iss).
WINDOWS_APP_ID = "{6F3C2B1E-8D4A-4E7B-9C52-1A0E7D3F5B94}"
WINDOWS_UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{WINDOWS_APP_ID}_is1"


class LayoutError(RuntimeError):
    """The installation on disk is not what the upgrader can safely work with."""


@dataclass(frozen=True)
class Installation:
    edition: str
    data_dir: Path
    workspace_dir: Path | None = None
    account: str | None = None       # Platform Client: its own OS account
    service: str | None = None       # Platform Client: its systemd unit
    port: int | None = None          # Platform Client: loopback UI port (health check)
    release_source: str | None = None

    def to_json(self) -> dict:
        return {"schema": 1, "edition": self.edition, "data_dir": str(self.data_dir),
                "workspace_dir": str(self.workspace_dir) if self.workspace_dir else None,
                "account": self.account, "service": self.service, "port": self.port,
                "release_source": self.release_source}

    @classmethod
    def from_json(cls, record: dict) -> "Installation":
        if not isinstance(record, dict) or record.get("schema") != 1 or record.get("edition") not in EDITIONS:
            raise LayoutError("Unrecognized installation record.")
        return cls(record["edition"], Path(record["data_dir"]),
                   Path(record["workspace_dir"]) if record.get("workspace_dir") else None,
                   record.get("account"), record.get("service"), record.get("port"),
                   record.get("release_source"))


def sync_windows_entry(prefix: Path, version: str) -> None:
    """Keep Apps & Features showing the active version. Every switch (install, upgrade, automatic or
    user rollback, recover) passes through ``set_current``; when the Windows installer registered
    this very installation (its per-user entry's InstallLocation is ``prefix``), the entry's version
    is set to ``version``. Display metadata only: a failure here never fails the switch."""
    if sys.platform != "win32":
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_UNINSTALL_KEY, 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            location = str(winreg.QueryValueEx(key, "InstallLocation")[0])
            if os.path.normcase(os.path.abspath(location)) != os.path.normcase(os.path.abspath(prefix)):
                return
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, version)
            major, minor = Version.parse(version).release[:2]
            winreg.SetValueEx(key, "VersionMajor", 0, winreg.REG_DWORD, major)
            winreg.SetValueEx(key, "VersionMinor", 0, winreg.REG_DWORD, minor)
    except OSError:
        return


def write_json_atomic(path: Path, record: dict, mode: int = 0o644) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    replace_file(tmp, path)
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    if sys.platform == "win32":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Layout:
    def __init__(self, prefix: Path):
        self.prefix = prefix
        self.versions = prefix / "versions"
        self.upgrade = prefix / "upgrade"
        self.bin = prefix / "bin"
        self.runtime = prefix / "runtime"   # Windows Platform Client: admin-owned interpreter copy
        self.state_file = self.upgrade / "state.json"
        self.history_file = self.upgrade / "history.jsonl"

    # -- identity ---------------------------------------------------------------

    def installation(self) -> Installation:
        try:
            record = json.loads((self.prefix / INSTALLATION).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise LayoutError(f"{self.prefix} is not a managed Nature AI Worker installation "
                              f"({INSTALLATION} not found).") from None
        except (OSError, ValueError) as exc:
            raise LayoutError("Unreadable installation record.") from exc
        return Installation.from_json(record)

    def write_installation(self, inst: Installation) -> None:
        write_json_atomic(self.prefix / INSTALLATION, inst.to_json())

    @property
    def local_keys(self) -> Path:
        return self.prefix / LOCAL_KEYS

    # -- versions -----------------------------------------------------------------

    def version_dir(self, version: str) -> Path:
        Version.parse(version)  # also rejects path separators
        return self.versions / version

    def python(self, version: str) -> Path:
        venv = self.version_dir(version) / "venv"
        return venv / "Scripts" / "python.exe" if sys.platform == "win32" else venv / "bin" / "python"

    def complete(self, version: str) -> bool:
        d = self.version_dir(version)
        return d.is_dir() and not (d / INCOMPLETE).exists() and (d / INSTALLED).is_file()

    def installed_record(self, version: str) -> dict:
        try:
            return json.loads((self.version_dir(version) / INSTALLED).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def installed_versions(self) -> list[str]:
        if not self.versions.is_dir():
            return []
        out = []
        for d in self.versions.iterdir():
            try:
                if self.complete(d.name):
                    out.append(d.name)
            except ReleaseError:
                continue
        return sorted(out, key=Version.parse)

    def current(self) -> str:
        try:
            version = read_text(self.prefix / CURRENT).strip()
        except FileNotFoundError:
            raise LayoutError("No active version (the 'current' pointer is missing).") from None
        try:
            if not self.complete(version):
                raise LayoutError(f"The active version {version} is not completely installed.")
        except ReleaseError as exc:
            raise LayoutError("The 'current' pointer is corrupt.") from exc
        return version

    def set_current(self, version: str) -> None:
        if not self.complete(version):
            raise LayoutError(f"Cannot activate {version}: it is not completely installed.")
        tmp = self.prefix / f".{CURRENT}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(version)  # no newline: read by `set /p` in the Windows launcher
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(tmp, self.prefix / CURRENT)
        _fsync_dir(self.prefix)
        sync_windows_entry(self.prefix, version)

    # -- journal and history ------------------------------------------------------

    def ensure_upgrade_dir(self) -> None:
        self.upgrade.mkdir(mode=0o755 if self.is_system() else 0o700, parents=True, exist_ok=True)

    def is_system(self) -> bool:
        try:
            return self.installation().edition == "platform_client"
        except LayoutError:
            return False  # not installed yet: private, like a Standalone installation

    def lock(self) -> RuntimeLock:
        self.ensure_upgrade_dir()
        lock = RuntimeLock(self.upgrade / "upgrade.lock")
        if not lock.acquire():
            raise LayoutError("Another upgrade of this installation is running.")
        return lock

    def state(self) -> dict | None:
        try:
            return json.loads(read_text(self.state_file))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise LayoutError("The upgrade journal is unreadable: run `nature-worker-upgrade recover`.") from exc

    def save_state(self, state: dict) -> None:
        self.ensure_upgrade_dir()
        write_json_atomic(self.state_file, {**state, "updated_at": time.time()})

    def clear_state(self) -> None:
        self.state_file.unlink(missing_ok=True)

    def record(self, event: dict) -> None:
        """Append one outcome to the upgrade history (no secrets are ever passed here)."""
        self.ensure_upgrade_dir()
        line = json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **event}) + "\n"
        fd = os.open(self.history_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())

    def history(self) -> list[dict]:
        try:
            lines = self.history_file.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"outcome": "unreadable-entry"})
        return out

    # -- launchers ----------------------------------------------------------------

    def write_launchers(self, inst: Installation) -> list[Path]:
        """Launchers that always run ``versions/<current>``; written at install time only."""
        self.bin.mkdir(mode=0o755, exist_ok=True)
        module = "nature_worker" if inst.edition == "standalone" else "nature_worker.platform_client"
        main = "nature-worker" if inst.edition == "standalone" else "nature-worker-platform-client"
        env = {PREFIX_ENV: str(self.prefix)}
        if inst.edition == "standalone":
            env["NATURE_WORKER_DATA_DIR"] = str(inst.data_dir)
            env["NATURE_WORKER_WORKSPACE"] = str(inst.workspace_dir)
        commands = [(main, module), ("nature-worker-upgrade", "nature_worker.upgrade")]
        windows_service = sys.platform == "win32" and inst.edition == "platform_client"
        if windows_service:
            commands.append(("nature-worker-platform-client-setup", "nature_worker.platform_client.windows_setup"))
        if sys.platform == "win32" and inst.edition == "standalone":
            commands.append(("nature-worker-desktop", "nature_worker.desktop"))   # the Start-menu entry
        written = []
        for name, mod in commands:
            if sys.platform == "win32":
                written.append(self._write_cmd(name, mod, env))
            else:
                written.append(self._write_sh(name, mod, env))
        if windows_service:
            written.append(self._write_service_bootstrap())
        return written

    def _write_service_bootstrap(self) -> Path:
        """The Windows service runs ``runtime\\python.exe`` on this file (a venv's python.exe
        is a launcher that starts a second process, which the SCM cannot supervise). Like
        the launchers, it always runs ``versions/<current>``."""
        body = f'''# Generated by the Platform Client installer. Runs the active version as the Windows service.
import re
import site
import sys
from pathlib import Path

PREFIX = Path({str(self.prefix)!r})
VERSION = (PREFIX / "current").read_text(encoding="utf-8").strip()
if not re.fullmatch(r"[0-9][0-9.]*[+a-z0-9.]*", VERSION):
    sys.exit("Invalid active version in " + str(PREFIX / "current"))
site.addsitedir(str(PREFIX / "versions" / VERSION / "venv" / "Lib" / "site-packages"))
from nature_worker.platform_client.winservice import main
sys.exit(main())
'''
        path = self.bin / "platform-client-service.py"
        path.write_text(body, encoding="utf-8")
        return path

    def _write_sh(self, name: str, module: str, env: dict) -> Path:
        q = shlex.quote
        exports = "".join(f"export {k}={q(v)}\n" for k, v in env.items())
        body = f"""#!/bin/sh
# Generated by nature-worker-upgrade. Runs the active version of this installation.
PREFIX={q(str(self.prefix))}
VERSION=$(cat "$PREFIX/current") || exit 1
case "$VERSION" in ''|*[!A-Za-z0-9.+]*) echo "Invalid active version in $PREFIX/current" >&2; exit 1 ;; esac
{exports}exec "$PREFIX/versions/$VERSION/venv/bin/python" -I -m {module} "$@"
"""
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        os.chmod(path, 0o755)
        return path

    def _write_cmd(self, name: str, module: str, env: dict) -> Path:
        for value in (str(self.prefix), *env.values()):
            if any(c in value for c in '%"^&|<>!\r\n'):
                raise LayoutError(f"Unsupported character in path {value!r} for a Windows launcher.")
        sets = "".join(f'set "{k}={v}"\r\n' for k, v in env.items())
        body = (
            "@echo off\r\n"
            "rem Generated by nature-worker-upgrade. Runs the active version of this installation.\r\n"
            "setlocal\r\n"
            f'set "PREFIX={self.prefix}"\r\n'
            'set "VERSION="\r\n'
            'set /p VERSION=<"%PREFIX%\\current"\r\n'
            'echo(%VERSION%| findstr /r /x "[0-9][0-9.]*[+a-z0-9.]*" >nul || '
            '(echo Invalid active version in %PREFIX%\\current 1>&2 & exit /b 1)\r\n'
            f"{sets}"
            f'"%PREFIX%\\versions\\%VERSION%\\venv\\Scripts\\python.exe" -I -m {module} %*\r\n'
        )
        path = self.bin / f"{name}.cmd"
        path.write_text(body, encoding="utf-8", newline="")
        return path


def find_prefix(explicit: Path | None = None) -> Path | None:
    """The managed installation this code belongs to, if any."""
    if explicit is not None:
        return explicit
    env = os.environ.get(PREFIX_ENV, "").strip()
    if env:
        return Path(env)
    # Running from <prefix>/versions/<version>/venv?
    venv = Path(sys.prefix)
    if venv.name == "venv" and venv.parent.parent.name == "versions":
        prefix = venv.parent.parent.parent
        if (prefix / INSTALLATION).is_file():
            return prefix
    return None


def installation_summary() -> dict:
    """What the Standalone web UI shows about this installation (read-only)."""
    prefix = find_prefix()
    if prefix is None:
        return {"managed": False}
    layout = Layout(prefix)
    summary: dict = {"managed": True}
    try:
        summary["active_version"] = layout.current()
        state = layout.state()
        summary["upgrade_in_progress"] = bool(state and state.get("phase") not in ("complete",))
        history = layout.history()
        summary["last_upgrade"] = {k: history[-1].get(k) for k in ("at", "outcome", "from", "to")} if history else None
        available = json.loads((layout.upgrade / "available.json").read_text(encoding="utf-8"))
        summary["available"] = {k: available.get(k) for k in
                                ("version", "official", "eligible", "reasons", "checked_at", "notes")}
    except (OSError, ValueError, LayoutError):
        pass
    return summary
