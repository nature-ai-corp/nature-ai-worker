# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Install, repair, uninstall and inspect the Windows Platform Client (S-C).

The Windows counterpart of scripts/platform-client-linux-install.sh, using
Windows mechanisms only (no WSL, Docker or Linux):

    Linux                                   Windows
    nature-pc system account                virtual account NT SERVICE\\NatureWorkerPlatformClient
    /opt/nature-worker-platform-client      %ProgramFiles%\\NatureAIWorker\\PlatformClient (admin-owned)
    /var/lib/... mode 0700                  %ProgramData%\\NatureAIWorker\\PlatformClient, protected DACL:
                                            SYSTEM, Administrators and the service account only
    systemd unit, EnvironmentFile           Windows service, registry Environment (admins only)
    system python3 (root-owned)             runtime\\ : a copy of the installing Python, admin-owned

The versioned layout is the one nature-worker-upgrade manages (docs/UPGRADES.md):
every build is a complete venv in versions\\<version>+g<commit>, ``current`` names
the active one, and signed releases are installed with
``bin\\nature-worker-upgrade.cmd upgrade --bundle DIR`` (as administrator).

The Standalone installation, its data and its user are never read or changed.
Builds made here are UNSIGNED local builds, never reported as official releases.

This module imports only the standard library and light Worker modules, so the
uninstaller can run from a temporary copy while it removes the installation.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

from .. import maintenance
from ..installation_mode import PLATFORM_CLIENT, ModeError, read_mode
from ..install_gate import CleanEvidence, standalone_install_decision
from ..windows_security import (
    ADMINISTRATORS, MACHINE_ADMINS, SYSTEM, Acl, WindowsSecurityError, admin_only_problems,
    private_problems, service_sid,
)
from .winservice import SERVICE_NAME, service_log

ACCOUNT = f"NT SERVICE\\{SERVICE_NAME}"
DISPLAY_NAME = "Nature AI Worker Platform Client (S-C)"
DESCRIPTION = ("Thin client of the NATURE AI Platform Client Interface. Loopback web UI; no local Agent. "
               "Runs as its own account with a private data folder.")
PUBLISHER = "The Nature AI Worker Authors"
SERVICE_KEY = rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}"
UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\NatureAIWorkerPlatformClient"
SHORTCUT = "Nature AI Worker Platform Client.url"
DEFAULT_PORT = 8090
DRAIN_TIMEOUT = 120
KEEP_OLD_BUILDS = 2
_VERSION = re.compile(r"^[0-9][0-9.]*[+a-z0-9.]*$")


class SetupError(RuntimeError):
    """A step failed or was refused; the message says what to do."""


def default_prefix() -> Path:
    return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NatureAIWorker" / "PlatformClient"


def default_data() -> Path:
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "NatureAIWorker" / "PlatformClient"


def start_menu() -> Path:
    return (Path(os.environ.get("ProgramData", r"C:\ProgramData"))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs")


class WindowsSystem:
    """Every OS operation of the installer (tests replace this with a recording fake)."""

    def token(self):
        from ..windows_security import current_token
        return current_token()

    def read_acl(self, path: Path) -> Acl:
        from ..windows_security import read_acl
        return read_acl(path)

    def is_link(self, path: Path) -> bool:
        """A symbolic link or a junction (any reparse point): never followed or trusted."""
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        return bool(getattr(info, "st_file_attributes", 0) & 0x400) or path.is_symlink()

    def run(self, cmd: list[str], *, env: dict | None = None, timeout: float = 1800,
            check: bool = True) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout,
                                    stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SetupError(f"{Path(cmd[0]).name} could not run: {type(exc).__name__}") from exc
        if check and result.returncode != 0:
            lines = [ln for ln in (result.stderr or result.stdout or "").splitlines() if ln.strip()]
            raise SetupError(f"{Path(cmd[0]).name} {' '.join(cmd[1:3])} failed (exit {result.returncode}): "
                             f"{lines[-1][:300] if lines else 'no output'}")
        return result

    def service_status(self):
        from .. import windows_service
        return windows_service.query(SERVICE_NAME)

    def start_service(self, timeout: float) -> None:
        from .. import windows_service
        try:
            windows_service.start(SERVICE_NAME, timeout)
        except windows_service.ServiceError as exc:
            raise SetupError(str(exc)) from exc

    def stop_service(self, timeout: float) -> None:
        from .. import windows_service
        try:
            windows_service.stop(SERVICE_NAME, timeout)
        except windows_service.ServiceError as exc:
            raise SetupError(str(exc)) from exc

    def set_registry(self, key: str, values: dict) -> None:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_SET_VALUE) as handle:
            for name, value in values.items():
                kind = (winreg.REG_MULTI_SZ if isinstance(value, list) else
                        winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ)
                winreg.SetValueEx(handle, name, 0, kind, value)

    def get_registry(self, key: str, name: str):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ) as handle:
                return winreg.QueryValueEx(handle, name)[0]
        except FileNotFoundError:
            return None

    def delete_registry_tree(self, key: str) -> None:
        import winreg
        try:
            winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, key)
        except FileNotFoundError:
            pass

    def http_status(self, port: int) -> dict | None:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/api/status", timeout=3) as response:
                return json.load(response)
        except (OSError, ValueError):
            return None

    def port_in_use(self, port: int) -> bool:
        import socket
        with socket.socket() as s:
            s.settimeout(1)
            return s.connect_ex(("127.0.0.1", port)) == 0

    def platform_client_processes(self) -> list[str] | None:
        """Command lines of running Platform Client processes, or None if unknown."""
        script = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "
                  "'nature_worker\\.platform_client|platform-client-service\\.py' -and "
                  "$_.ProcessId -ne $PID } | ForEach-Object { [string]$_.ProcessId + ' ' + $_.CommandLine }")
        try:
            result = self.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                              timeout=120)
        except SetupError:
            return None
        own = str(os.getpid())
        return [ln.strip() for ln in result.stdout.splitlines() if ln.strip() and ln.split()[0] != own]

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class Setup:
    def __init__(self, prefix: Path | None = None, data: Path | None = None, *,
                 system: WindowsSystem | None = None, say=print):
        self.prefix = (prefix or default_prefix()).absolute()
        self.data = (data or default_data()).absolute()
        self.system = system or WindowsSystem()
        self.say = say
        self.sid = service_sid(SERVICE_NAME)

    # -- paths ------------------------------------------------------------------------

    @property
    def runtime_python(self) -> Path:
        return self.prefix / "runtime" / "python.exe"

    def version_python(self, label: str) -> Path:
        return self.prefix / "versions" / label / "venv" / "Scripts" / "python.exe"

    def current(self) -> str | None:
        try:
            label = (self.prefix / "current").read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return label if _VERSION.match(label) else None

    def installation(self) -> dict | None:
        try:
            return json.loads((self.prefix / "installation.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # -- checks -----------------------------------------------------------------------

    def require_admin(self) -> None:
        token = self.system.token()
        if not (token.administrator and (token.elevated or token.user == SYSTEM)):
            raise SetupError("Run this as administrator (an elevated prompt): the Platform Client is a "
                             "Windows service with code under Program Files.")

    def standalone_sid(self, standalone_data: Path | None) -> str:
        """The Windows account of the Standalone user: the owner of its data root when that is a
        user account, otherwise the account running this installer (the person installing)."""
        if standalone_data is not None and standalone_data.exists():
            if self.system.is_link(standalone_data):
                raise SetupError(f"{standalone_data} is a link; give the Standalone data folder itself.")
            owner = self.system.read_acl(standalone_data).owner
            if owner not in MACHINE_ADMINS and owner != self.sid:
                return owner
        return self.system.token().user

    def check_data_root(self) -> None:
        for path in (self.data.parent, self.data):
            if self.system.is_link(path):
                raise SetupError(f"{path} is a link or junction; refusing.")
            if path.exists():
                owner = self.system.read_acl(path).owner
                if owner not in MACHINE_ADMINS:
                    raise SetupError(f"{path} was created by a non-administrator ({owner}). It cannot be "
                                     "trusted as Platform Client storage; remove it and run the installer again.")
        if self.data.exists() and any(self.data.iterdir()):
            try:
                mode = read_mode(self.data)
            except ModeError as exc:
                raise SetupError(f"{self.data}: {exc}") from exc
            if mode != PLATFORM_CLIENT:
                raise SetupError(f"{self.data} exists and is not a Platform Client installation; refusing.")

    def check_port(self, port: int) -> None:
        status = self.system.service_status()
        ours = status is not None and status.state == 4 and self._registered_port() == port
        if not ours and self.system.port_in_use(port):
            raise SetupError(f"127.0.0.1:{port} is already used by another program; choose another --port.")

    def _registered_port(self) -> int | None:
        env = self.service_environment()
        try:
            return int(env.get("NWPC_PORT", ""))
        except ValueError:
            return None

    def service_environment(self) -> dict[str, str]:
        values = self.system.get_registry(SERVICE_KEY, "Environment") or []
        return dict(v.split("=", 1) for v in values if "=" in v)

    # -- install ----------------------------------------------------------------------

    def install(self, *, origin: str, standalone_data: Path | None, port: int = DEFAULT_PORT,
                archive: Path | None = None, source: Path | None = None,
                allow_insecure_loopback: bool = False) -> str:
        self.require_admin()
        _check_origin(origin, allow_insecure_loopback)
        standalone_sid = self.standalone_sid(standalone_data)
        if standalone_sid == self.sid:
            raise SetupError("The Standalone user cannot be the Platform Client service account.")
        self.check_data_root()
        self.check_port(port)
        previous = self.current()
        status = self.system.service_status()
        was_running = status is not None and status.state != 1
        self.stop_service(drain=True)

        staging = self.prefix / "upgrade" / "staging" / "windows-install"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        label = None
        try:
            commit, src = self.stage_source(staging, archive=archive, source=source)
            version = _source_version(src)
            label = f"{version}+g{commit[:12]}"
            self.ensure_runtime()
            self.build_version(label, src)
            self.provision_data_root()
            self.say(f"Preparing the data root {self.data}")
            self.system.run([str(self.version_python(label)), "-I", "-m", "nature_worker.platform_client",
                             "init"], env=_child_env({"NWPC_DATA_DIR": str(self.data)}))
            self.reset_children(self.data)
            self.system.run([str(self.version_python(label)), "-I", "-m", "nature_worker.upgrade",
                             "register-local-build", "--prefix", str(self.prefix), "--version", label,
                             "--edition", "platform_client", "--data-dir", str(self.data),
                             "--account", ACCOUNT, "--service", SERVICE_NAME, "--port", str(port),
                             "--commit", commit], env=_child_env())
            (self.prefix / "COMMIT").write_text(commit + "\n", encoding="utf-8")
            self.configure_service(origin=origin, standalone_data=standalone_data,
                                   standalone_sid=standalone_sid, port=port,
                                   allow_insecure_loopback=allow_insecure_loopback)
            self.start_and_verify(label, origin, port)
        except (SetupError, OSError):
            # Whatever failed, an installation that was serving before serves again.
            if previous and previous != label and (self.current() != previous or was_running):
                self.reactivate(previous, port)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self.register_app(label, port)
        self.prune_builds(label)
        self.say(f"Platform Client running {label}: open http://127.0.0.1:{port}/ and sign in.")
        return label

    def stage_source(self, staging: Path, *, archive: Path | None, source: Path | None) -> tuple[str, Path]:
        """Exactly one committed tree (``git archive``), never the working copy."""
        tar = staging / "src.tar"
        if archive is None:
            source = source or Path(__file__).resolve().parents[3]
            self.system.run(["git", "-C", str(source), "archive", "--format=tar", "-o", str(tar), "HEAD"])
            dirty = self.system.run(["git", "-C", str(source), "diff", "--quiet", "HEAD", "--"], check=False)
            if dirty.returncode != 0:
                self.say(f"note: uncommitted changes in {source} are NOT installed")
        else:
            shutil.copyfile(archive, tar)
        with tarfile.open(tar) as t:
            commit = (t.pax_headers or {}).get("comment", "")
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise SetupError("The source archive does not name its commit (make it with `git archive`).")
            target = staging / "src"
            target.mkdir()
            t.extractall(target, filter="data")
        self.say(f"Installing commit {commit}")
        return commit, target

    def ensure_runtime(self) -> None:
        """An admin-owned interpreter the service account can read and no user can change."""
        runtime = self.runtime_python.parent
        wanted = f"{sys.version_info[0]}.{sys.version_info[1]}"
        if self.runtime_python.is_file():
            have = self.system.run([str(self.runtime_python), "-I", "-c",
                                    "import sys; print('%d.%d' % sys.version_info[:2])"]).stdout.strip()
            if have == wanted:
                return
            raise SetupError(f"The installed runtime is Python {have}; run the installer with Python {have}.")
        base = Path(sys.base_prefix).resolve()
        if self.prefix in base.parents or base == self.prefix:
            raise SetupError("The runtime is missing from this installation; reinstall it.")
        if "WindowsApps" in base.parts:
            raise SetupError("Python from the Microsoft Store cannot be copied for a service. Install Python "
                             "from python.org and run the installer with it.")
        self.say(f"Copying Python {wanted} from {base} to {runtime}")
        shutil.copytree(base, runtime, ignore=shutil.ignore_patterns(
            "site-packages", "Scripts", "Doc", "Tools", "test", "tests", "idlelib", "tkinter",
            "turtledemo", "tcl", "__pycache__", "*.pyc"))
        (runtime / "Lib" / "site-packages").mkdir(exist_ok=True)

    def build_version(self, label: str, src: Path) -> None:
        target = self.prefix / "versions" / label
        if (target / "INSTALLED.json").is_file() and not (target / ".incomplete").exists():
            self.say(f"{label} is already built")
            return
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        (target / ".incomplete").write_text("installation in progress\n", encoding="utf-8")
        self.say(f"Building {label} in {target}")
        env = _child_env()
        self.system.run([str(self.runtime_python), "-I", "-m", "venv", str(target / "venv")], env=env)
        python = str(self.version_python(label))
        self.system.run([python, "-I", "-m", "pip", "install", "-q", "--disable-pip-version-check",
                         "--no-input", "--only-binary=:all:", "-r", str(src / "requirements.lock")], env=env)
        self.system.run([python, "-I", "-m", "pip", "install", "-q", "--disable-pip-version-check",
                         "--no-input", "--no-deps", str(src)], env=env)

    def provision_data_root(self) -> None:
        """Parent: administrators only. Data root: also the service account (modify), nothing else."""
        parent = self.data.parent
        self.data.mkdir(parents=True, exist_ok=True)
        icacls = ["icacls"]
        self.system.run([*icacls, str(parent), "/setowner", f"*{ADMINISTRATORS}"])
        self.system.run([*icacls, str(parent), "/inheritance:r", "/grant:r",
                         f"*{SYSTEM}:(OI)(CI)F", f"*{ADMINISTRATORS}:(OI)(CI)F"])
        self.system.run([*icacls, str(self.data), "/setowner", f"*{ADMINISTRATORS}"])
        self.system.run([*icacls, str(self.data), "/inheritance:r", "/grant:r",
                         f"*{SYSTEM}:(OI)(CI)F", f"*{ADMINISTRATORS}:(OI)(CI)F", f"*{self.sid}:(OI)(CI)M"])
        self.reset_children(self.data)
        problems = private_problems(self.system.read_acl(self.data), self.sid)
        if problems:
            raise SetupError(f"{self.data} is not private to {ACCOUNT}: " + "; ".join(problems))

    def reset_children(self, directory: Path) -> None:
        """Files inside the data root take exactly its permissions (never the root itself)."""
        if directory.is_dir() and any(directory.iterdir()):
            self.system.run(["icacls", str(directory / "*"), "/reset", "/T", "/C", "/Q"])

    def configure_service(self, *, origin: str, standalone_data: Path | None, standalone_sid: str,
                          port: int, allow_insecure_loopback: bool) -> None:
        bootstrap = self.prefix / "bin" / "platform-client-service.py"
        bin_path = f'"{self.runtime_python}" -I "{bootstrap}"'
        verb = "config" if self.system.service_status() is not None else "create"
        self.say(f"{'Updating' if verb == 'config' else 'Creating'} the {SERVICE_NAME} service ({ACCOUNT})")
        self.system.run(["sc.exe", verb, SERVICE_NAME, "binPath=", bin_path, "start=", "delayed-auto",
                         "obj=", ACCOUNT, "DisplayName=", DISPLAY_NAME])
        self.system.run(["sc.exe", "description", SERVICE_NAME, DESCRIPTION])
        self.system.run(["sc.exe", "sidtype", SERVICE_NAME, "unrestricted"])
        # Only the privilege every process needs (traverse folders): nothing else.
        self.system.run(["sc.exe", "privs", SERVICE_NAME, "SeChangeNotifyPrivilege"])
        self.system.run(["sc.exe", "failure", SERVICE_NAME, "reset=", "86400",
                         "actions=", "restart/10000/restart/30000"])
        env = {
            "NWPC_DATA_DIR": str(self.data),
            "NWPC_PLATFORM_ORIGIN": origin,
            "NWPC_STANDALONE_DATA_DIR": str(standalone_data) if standalone_data else "",
            "NWPC_STANDALONE_SID": standalone_sid,
            "NWPC_HOST": "127.0.0.1",
            "NWPC_PORT": str(port),
            "NATURE_WORKER_INSTALL_PREFIX": str(self.prefix),
            "PYTHONUTF8": "1",
        }
        if allow_insecure_loopback:
            env["NWPC_ALLOW_INSECURE_LOOPBACK"] = "1"
        self.system.set_registry(SERVICE_KEY, {"Environment": [f"{k}={v}" for k, v in env.items()]})

    def start_and_verify(self, label: str, origin: str, port: int) -> None:
        self.say(f"Starting {SERVICE_NAME}")
        try:
            self.system.start_service(90)
        except SetupError as exc:
            raise SetupError(f"{exc}\n{self.log_tail()}") from exc
        for _ in range(60):
            status = self.system.http_status(port)
            if status and status.get("version") == label and status.get("platform") == origin:
                return
            self.system.sleep(1)
        raise SetupError(f"The service did not answer as {label} on 127.0.0.1:{port}.\n{self.log_tail()}")

    def reactivate(self, previous: str, port: int) -> None:
        """After a failed reinstall, the build that was active before is active and serving again."""
        self.say(f"The installation did not complete; reactivating {previous}")
        try:
            self.system.stop_service(60)
            if self.current() != previous:
                tmp = self.prefix / f".current.{os.getpid()}.tmp"
                tmp.write_text(previous, encoding="utf-8")
                os.replace(tmp, self.prefix / "current")
            self.system.start_service(90)
            for _ in range(60):
                status = self.system.http_status(port)
                if status and status.get("version") == previous:
                    self.say(f"{previous} is running again.")
                    return
                self.system.sleep(1)
            raise SetupError(f"{previous} did not answer on 127.0.0.1:{port}")
        except (SetupError, OSError) as exc:
            self.say(f"Reactivating {previous} failed too ({exc}). Run `repair` after fixing the cause.")

    def log_tail(self, lines: int = 30) -> str:
        try:
            text = service_log(self.data).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return f"(no service log at {service_log(self.data)})"
        return f"Last lines of {service_log(self.data)}:\n  " + "\n  ".join(text[-lines:])

    def register_app(self, label: str, port: int) -> None:
        setup = self.prefix / "bin" / "nature-worker-platform-client-setup.cmd"
        self.system.set_registry(UNINSTALL_KEY, {
            "DisplayName": DISPLAY_NAME, "DisplayVersion": label, "Publisher": PUBLISHER,
            "InstallLocation": str(self.prefix), "UninstallString": f'"{setup}" uninstall',
            "ModifyPath": f'"{setup}" repair', "NoRepair": 1,
            "URLInfoAbout": "https://github.com/nature-ai-corp/nature-ai-worker",
        })
        shortcut = start_menu() / SHORTCUT
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        shortcut.write_text(f"[InternetShortcut]\r\nURL=http://127.0.0.1:{port}/\r\n", encoding="utf-8")

    def prune_builds(self, active: str) -> None:
        versions = self.prefix / "versions"
        old = sorted((d for d in versions.iterdir() if d.name != active and "+g" in d.name),
                     key=lambda d: d.stat().st_mtime, reverse=True)
        for directory in old[KEEP_OLD_BUILDS:]:
            shutil.rmtree(directory, ignore_errors=True)

    def stop_service(self, *, drain: bool) -> None:
        """Let running submissions finish (the upgrade drain protocol), then stop; the
        service's shutdown signs out of the Platform."""
        status = self.system.service_status()
        if status is None or status.state == 1:
            return
        if drain and self.data.is_dir():
            self.say("Asking the Platform Client to finish running submissions and sign out")
            maintenance.request_stop(self.data, "windows setup")
            for _ in range(DRAIN_TIMEOUT * 2):
                status = self.system.service_status()
                if status is None or status.state == 1:
                    break
                self.system.sleep(0.5)
            maintenance.withdraw_stop(self.data)
        self.system.stop_service(60)

    # -- repair -----------------------------------------------------------------------

    def repair(self) -> str:
        """Re-apply service, permissions, launchers and registration for the active build.
        Never changes the version, the Platform origin or the data."""
        self.require_admin()
        inst, label = self.installation(), self.current()
        if not inst or inst.get("edition") != PLATFORM_CLIENT or not label:
            raise SetupError(f"{self.prefix} is not an intact Platform Client installation; install it again.")
        if Path(inst["data_dir"]).absolute() != self.data:
            raise SetupError(f"The installation uses the data root {inst['data_dir']}; pass --data.")
        env = self.service_environment()
        origin = env.get("NWPC_PLATFORM_ORIGIN")
        if not origin:
            raise SetupError("The service configuration is missing; install again with --origin.")
        if not self.runtime_python.is_file() or not self.version_python(label).is_file():
            raise SetupError(f"The files of {label} are missing; install again.")
        problems = admin_only_problems(self.system.read_acl(self.prefix))
        if problems:
            raise SetupError(f"{self.prefix} can be changed by non-administrators ({'; '.join(problems)}). "
                             "Uninstall and install again.")
        self.check_data_root()
        self.stop_service(drain=True)
        self.provision_data_root()
        if read_mode(self.data) != PLATFORM_CLIENT:
            self.system.run([str(self.version_python(label)), "-I", "-m", "nature_worker.platform_client",
                             "init"], env=_child_env({"NWPC_DATA_DIR": str(self.data)}))
            self.reset_children(self.data)
        self.system.run([str(self.version_python(label)), "-I", "-c",
                         "import sys; from pathlib import Path; from nature_worker.upgrade.layout import Layout; "
                         "l = Layout(Path(sys.argv[1])); l.write_launchers(l.installation())", str(self.prefix)],
                        env=_child_env())
        port = int(env.get("NWPC_PORT") or inst.get("port") or DEFAULT_PORT)
        standalone = env.get("NWPC_STANDALONE_DATA_DIR") or None
        self.configure_service(origin=origin, standalone_data=Path(standalone) if standalone else None,
                               standalone_sid=env.get("NWPC_STANDALONE_SID") or self.system.token().user,
                               port=port, allow_insecure_loopback=env.get("NWPC_ALLOW_INSECURE_LOOPBACK") == "1")
        self.start_and_verify(label, origin, port)
        self.register_app(label, port)
        self.say(f"Repaired: {label} running on http://127.0.0.1:{port}/")
        return label

    # -- uninstall --------------------------------------------------------------------

    def uninstall(self, *, delete_local_data: bool) -> dict:
        """Remove the service, code and registration. Local data only with ``delete_local_data``,
        after its manifest is shown. Platform server data and Standalone are never touched."""
        self.require_admin()
        inst = self.installation() or {}
        port = int(self.service_environment().get("NWPC_PORT") or inst.get("port") or DEFAULT_PORT)
        failures: list[str] = []
        self.stop_service(drain=True)
        if self.system.service_status() is not None:
            self.system.run(["sc.exe", "delete", SERVICE_NAME], check=False)
            for _ in range(60):
                if self.system.service_status() is None:
                    break
                self.system.sleep(0.5)
            if self.system.service_status() is not None:
                failures.append(f"service {SERVICE_NAME} is still registered (close the Services window)")
        self.system.delete_registry_tree(UNINSTALL_KEY)
        (start_menu() / SHORTCUT).unlink(missing_ok=True)
        if self.prefix.exists() and not _remove_tree(self.prefix, self.system):
            failures.append(f"{self.prefix} could not be removed completely")

        self.say("Local Platform Client data manifest:")
        for line in _manifest(self.data, self.system):
            self.say(f"  {line}")
        if delete_local_data and self.data.exists():
            if not _remove_tree(self.data, self.system):
                failures.append(f"{self.data} could not be removed completely")
            parent = self.data.parent
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        elif self.data.exists():
            self.say("Data kept. Run `uninstall --delete-local-data` to remove it.")
        report = self.clean_validation(port)
        report["failures"] = failures
        for failure in failures:
            self.say(f"NOT DONE: {failure}")
        return report

    def clean_validation(self, port: int) -> dict:
        """Independent check after uninstall. A Standalone installation is allowed only if every
        probe is verified clean; anything unknown blocks (docs/PLATFORM_CLIENT_SECURITY.md)."""
        processes = self.system.platform_client_processes()
        running = None if processes is None else bool(processes) or self.system.port_in_use(port)
        registered = (self.system.service_status() is not None
                      or self.system.get_registry(UNINSTALL_KEY, "DisplayName") is not None)
        leftovers = [p for p in (self.prefix, self.data, start_menu() / SHORTCUT) if p.exists()]
        checks = {
            # The Windows Platform Client keeps sessions in memory only: it creates no Credential
            # Manager entry and no DPAPI file; its data root (checked below) is the only storage.
            "os_credentials": True,
            "running_processes": None if running is None else not running,
            "installed_packages": not registered,
            "other_app_locations": not leftovers,
            "device_policy": None,  # enterprise device policy cannot be verified from here
        }
        decision = standalone_install_decision(self.data, CleanEvidence(checks),
                                               mode_marker=self.data / "installation-mode.json")
        self.say("Clean validation: " + ("passed" if decision.allowed else "a fresh Standalone "
                 "installation is NOT authorized yet"))
        for check, value in checks.items():
            self.say(f"  {check}: {'verified clean' if value is True else 'residue' if value is False else 'unverified'}")
        for blocker in decision.blockers:
            self.say(f"  blocker: {blocker}")
        return {"checks": checks, "standalone_install_allowed": decision.allowed,
                "blockers": list(decision.blockers), "processes": processes or []}

    # -- status -----------------------------------------------------------------------

    def status(self) -> dict:
        inst, label = self.installation(), self.current()
        env = self.service_environment() if sys.platform == "win32" else {}
        port = int(env.get("NWPC_PORT") or (inst or {}).get("port") or DEFAULT_PORT)
        service = self.system.service_status()
        info = {
            "installed": inst is not None, "prefix": str(self.prefix), "data_root": str(self.data),
            "active_version": label, "service": service.name if service else "not installed",
            "account": ACCOUNT, "platform": env.get("NWPC_PLATFORM_ORIGIN"), "port": port,
            "answering": self.system.http_status(port),
        }
        try:
            info["data_root_private"] = not private_problems(self.system.read_acl(self.data), self.sid)
        except (OSError, WindowsSecurityError):
            info["data_root_private"] = None
        return info


# -- helpers --------------------------------------------------------------------------

def _check_origin(origin: str, allow_insecure_loopback: bool) -> None:
    if origin.startswith("https://"):
        return
    if allow_insecure_loopback and re.match(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/?$", origin):
        return
    raise SetupError("--origin must be an https:// Platform origin.")


def _source_version(src: Path) -> str:
    text = (src / "src" / "nature_worker" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', text, re.M)
    if not match:
        raise SetupError("The source tree has no __version__.")
    return match.group(1)


def _child_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("PIP_", "PYTHON", "NATURE_WORKER_", "NWPC_", "VIRTUAL_ENV"))}
    env["PYTHONUTF8"] = "1"
    env.update(extra or {})
    return env


def _manifest(root: Path, system: WindowsSystem) -> list[str]:
    if not root.exists():
        return ["(none)"]
    lines = []
    for directory, dirs, files in os.walk(root):
        here = Path(directory)
        for name in sorted(dirs):
            lines.append(f"{'<link>' if system.is_link(here / name) else '<dir> '} {here / name}")
        dirs[:] = [d for d in dirs if not system.is_link(here / d)]  # never descend into links
        for name in sorted(files):
            try:
                size = (here / name).lstat().st_size
            except OSError:
                size = -1
            lines.append(f"{size:>10} {here / name}")
    return lines or ["(empty)"]


def _remove_tree(root: Path, system: WindowsSystem) -> bool:
    """Delete ``root``; a link inside is removed as a link, never followed."""
    def onerror(func, path, _exc):
        try:
            os.chmod(path, 0o700)
            func(path)
        except OSError:
            pass
    for directory, dirs, _files in os.walk(root, topdown=True):
        for name in list(dirs):
            path = Path(directory) / name
            if system.is_link(path):
                # A Windows directory link or junction is removed with rmdir; anything else is unlinked.
                os.rmdir(path) if os.name == "nt" and path.is_dir() else path.unlink()
                dirs.remove(name)
    for attempt in range(5):
        shutil.rmtree(root, onerror=onerror)
        if not root.exists():
            return True
        time.sleep(1 + attempt)
    return False


# -- running from a copy, elevation ---------------------------------------------------

def _inside(path: Path, prefix: Path) -> bool:
    try:
        path.resolve().relative_to(prefix.resolve())
        return True
    except ValueError:
        return False


def _relocate(prefix: Path, argv: list[str]) -> int:
    """Uninstall cannot delete the files it runs from: continue from a temporary copy of the
    interpreter and of this package, in a new window, after this process has exited."""
    temp = Path(tempfile.mkdtemp(prefix="nwpc-uninstall-"))
    shutil.copytree(Path(sys.base_prefix), temp / "python",
                    ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc", "Doc", "test", "tests"))
    package = Path(__file__).resolve().parents[1]
    shutil.copytree(package, temp / "lib" / "nature_worker", ignore=shutil.ignore_patterns("__pycache__"))
    bootstrap = (f"import sys; sys.path.insert(0, {str(temp / 'lib')!r}); "
                 "from nature_worker.platform_client.windows_setup import main; sys.exit(main(sys.argv[1:]))")
    args = [str(temp / "python" / "python.exe"), "-I", "-c", bootstrap, *argv,
            "--wait-pid", str(os.getpid()), "--cleanup", str(temp), "--pause"]
    subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0), close_fds=True)
    print("The uninstall continues in a new window.")
    return 0


def _wait_pid(pid: int, timeout: float = 60) -> None:
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if handle:
        kernel32.WaitForSingleObject(handle, int(timeout * 1000))
        kernel32.CloseHandle(handle)


def _schedule_cleanup(temp: Path) -> None:
    subprocess.Popen(f'cmd /c "ping -n 4 127.0.0.1 >nul & rmdir /s /q "{temp}""',
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)


def _elevate(argv: list[str]) -> int:
    """Run this command again with a UAC prompt, in a new window that stays open at the end."""
    import ctypes
    main_module = sys.modules.get("__main__")
    spec = getattr(main_module, "__spec__", None)
    entry = ["-m", spec.name] if spec is not None else [str(Path(sys.argv[0]).resolve())]
    params = subprocess.list2cmdline(["-I", *entry, *argv, "--pause"])
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    if result <= 32:
        print("Administrator rights were not granted; nothing was changed.", file=sys.stderr)
        return 2
    print("Continuing in a new administrator window.")
    return 0


# -- command line ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="nature-worker-platform-client-setup",
                                     description="Windows Platform Client (S-C) installer.")
    parser.add_argument("--prefix", type=Path, help=f"installation folder (default {default_prefix()})")
    parser.add_argument("--data", type=Path, help=f"data root (default {default_data()})")
    parser.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--wait-pid", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--cleanup", type=Path, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("install", help="install or reinstall from a committed source tree")
    p.add_argument("--origin", required=True, help="https:// origin of the NATURE AI Platform")
    p.add_argument("--standalone-data", type=Path, help="Standalone data folder (identifies its Windows user)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--archive", type=Path, help="tar made by `git archive HEAD` (default: this checkout's HEAD)")
    p.add_argument("--source", type=Path, help="git checkout to install HEAD from")
    p.add_argument("--allow-insecure-loopback", action="store_true", help="development only: http://127.0.0.1")
    sub.add_parser("repair", help="re-apply service, permissions and registration of the active build")
    p = sub.add_parser("uninstall", help="remove the service and code; data only with --delete-local-data")
    p.add_argument("--delete-local-data", action="store_true")
    sub.add_parser("status", help="show the installation and service state")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("This installer is for Windows. On Linux use scripts/platform-client-linux-install.sh.",
              file=sys.stderr)
        return 2
    setup = Setup(args.prefix, args.data)
    code = 1
    try:
        if args.wait_pid:
            _wait_pid(args.wait_pid)
        if args.command != "status" and not _is_admin(setup):
            return _elevate([a for a in argv if a != "--pause"])
        if args.command == "uninstall" and _inside(Path(sys.executable), setup.prefix):
            return _relocate(setup.prefix, [a for a in argv if a != "--pause"])
        if args.command == "install":
            setup.install(origin=args.origin, standalone_data=args.standalone_data, port=args.port,
                          archive=args.archive, source=args.source,
                          allow_insecure_loopback=args.allow_insecure_loopback)
        elif args.command == "repair":
            setup.repair()
        elif args.command == "uninstall":
            report = setup.uninstall(delete_local_data=args.delete_local_data)
            code = 1 if report["failures"] else 0
            return code
        else:
            print(json.dumps(setup.status(), indent=2, default=str))
        code = 0
    except (SetupError, ModeError, WindowsSecurityError, OSError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        code = 1
    finally:
        if args.cleanup:
            _schedule_cleanup(args.cleanup)
        if args.pause:
            input("\nPress Enter to close this window.")
    return code


def _is_admin(setup: Setup) -> bool:
    try:
        setup.require_admin()
        return True
    except SetupError:
        return False


if __name__ == "__main__":
    sys.exit(main())
