# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""OS operations used by the upgrade engine, one toolkit per edition.

Standalone: everything runs as the user who owns the installation, never as
root. The Worker is a user process; it is asked to stop through its data root
and is not restarted by the upgrader (the user starts it, as before).

Platform Client (Windows): the same, with the Windows service, an elevated
upgrader and NTFS permissions instead of systemd, root and file ownership.

Platform Client (Linux): the upgrader runs as root because the code is
root-owned. Data-root work (schema, migrations, backups) runs as, or is handed
back to, the Platform Client's own account; the service is controlled with
systemctl. Standalone data and accounts are never touched.

Questions about a version (schema, Client Interface) are answered by running
that version's own installed code, so the answer always matches the code.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .. import maintenance
from ..installation_mode import read_mode
from ..migrations import backup_data_root, check_backup_space, prune_backups, restore_data_root
from .layout import INCOMPLETE, INSTALLED, Installation, Layout, LayoutError
from .release import VerifiedRelease, wheel_requirement

HEALTH_TIMEOUT = 60.0
# How long backup/restore wait for a process that still holds the data root
# (a migration left running by an interrupted upgrade finishes on its own).
DATA_ROOT_WAIT = 600.0


class ToolkitError(RuntimeError):
    """An OS step failed; the message is safe to show and to record."""


class StopRefused(ToolkitError):
    """The running Worker did not finish its work in time; nothing was changed."""


def host_target() -> dict:
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    system = "windows" if sys.platform == "win32" else sys.platform.rstrip("0123456789")
    return {"os": system, "arch": arch, "python": f"{sys.version_info[0]}.{sys.version_info[1]}"}


def clean_env(extra: dict | None = None) -> dict:
    """Environment for child processes: no pip/Python/Worker overrides from the caller."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PIP_", "PYTHON", "NATURE_WORKER_", "NWPC_", "VIRTUAL_ENV"))}
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PYTHONUTF8"] = "1"
    env.update(extra or {})
    return env


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Runner:
    """Runs a command; ``user`` runs it as another account (root only, Linux)."""

    def run(self, cmd: list[str], *, user: str | None = None, env: dict | None = None,
            timeout: float = 900) -> subprocess.CompletedProcess:
        if user is not None:
            cmd = ["runuser", "-u", user, "--", *cmd]
        try:
            result = subprocess.run(cmd, env=env or clean_env(), capture_output=True, text=True,
                                    timeout=timeout, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolkitError(f"{Path(cmd[0]).name} could not run: {type(exc).__name__}") from exc
        if result.returncode != 0:
            raise ToolkitError(f"{Path(cmd[0]).name} {' '.join(cmd[1:4])} failed "
                               f"(exit {result.returncode}): {_last_line(result.stderr or result.stdout)}")
        return result


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:300] if lines else "no output"


class BaseToolkit:
    edition = ""

    def __init__(self, layout: Layout, inst: Installation, *, runner: Runner | None = None,
                 progress: Callable[[str], None] = lambda _m: None):
        self.layout, self.inst = layout, inst
        self.runner = runner or Runner()
        self.progress = progress
        self.base_python = Path(getattr(sys, "_base_executable", sys.executable))

    data_user: str | None = None  # account that owns the data root (None: this account)

    def host_target(self) -> dict:
        return host_target()

    # -- installing a version --------------------------------------------------

    def install_version(self, release: VerifiedRelease) -> None:
        version = str(release.manifest.version)
        target = self.layout.version_dir(version)
        if self.layout.complete(version):
            if self.layout.installed_record(version).get("manifest_sha256") == release.manifest.digest:
                return
            raise LayoutError(f"Version {version} is already installed from a different release manifest.")
        if target.exists():
            shutil.rmtree(target)  # an earlier attempt that never completed; never an active version
        target.mkdir(parents=True)
        (target / INCOMPLETE).write_text("installation in progress\n")
        self.progress(f"Installing {version} into {target}")
        self.runner.run([str(self.base_python), "-I", "-m", "venv", str(target / "venv")])
        python = str(self.layout.python(version))
        requirements = target / "requirements.txt"
        requirements.write_text("".join(f"{wheel_requirement(a.path)} --hash=sha256:{a.sha256}\n"
                                        for _, a in release.wheels()), encoding="utf-8")
        # Only the verified wheels: no index, no source builds, every file hash-checked by pip too.
        self.runner.run([python, "-I", "-m", "pip", "install", "--no-index", "--find-links",
                         str(release.bundle / "wheels"), "--only-binary=:all:", "--require-hashes",
                         "--no-cache-dir", "--disable-pip-version-check", "--no-input", "-q",
                         "-r", str(requirements)])
        installed = self.runner.run([python, "-I", "-c",
                                     "import nature_worker; print(nature_worker.__version__)"]).stdout.strip()
        if installed != version:
            raise ToolkitError(f"The installed code reports version {installed!r}, not {version}.")
        shutil.copyfile(release.bundle / release.manifest.notes.path, target / "RELEASE_NOTES.md")
        (target / INSTALLED).write_text(json.dumps({
            "version": version, "manifest_sha256": release.manifest.digest,
            "signer_key_id": release.signer.key_id, "signer_scope": release.signer.scope,
            "source": release.manifest.source, "data_schema": release.manifest.data_schema,
            "client_interface": release.manifest.client_interface, "installed_at": time.time(),
        }, indent=2) + "\n", encoding="utf-8")
        (target / INCOMPLETE).unlink()

    def remove_incomplete(self, version: str) -> None:
        target = self.layout.version_dir(version)
        if target.exists() and not self.layout.complete(version):
            shutil.rmtree(target)

    # -- asking a version about itself -----------------------------------------------

    def _ask(self, version: str, args: list[str], as_data_user: bool = False) -> dict:
        python = str(self.layout.python(version))
        out = self.runner.run([python, "-I", *args], user=self.data_user if as_data_user else None).stdout
        try:
            return json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise ToolkitError(f"Version {version} gave an unreadable answer.") from exc

    def code_schema(self, version: str) -> int:
        return int(self._ask(version, ["-m", "nature_worker.migrations", "code-schema",
                                       "--edition", self.edition])["code_schema"])

    def data_schema(self, version: str) -> int | None:
        return self._ask(version, ["-m", "nature_worker.migrations", "data-schema", "--edition",
                                   self.edition, "--data-dir", str(self.inst.data_dir)],
                         as_data_user=True)["data_schema"]

    def client_interface(self, version: str) -> str:
        return self._ask(version, ["-c", "import json; from nature_worker.platform_contract import "
                                         "SUPPORTED_VERSION as v; print(json.dumps({'v': v}))"])["v"]

    def migrate(self, version: str) -> None:
        self.progress("Applying data migrations")
        self._ask(version, ["-m", "nature_worker.migrations", "migrate", "--no-backup", "--edition",
                            self.edition, "--data-dir", str(self.inst.data_dir)], as_data_user=True)

    # -- data root --------------------------------------------------------------------

    def data_mode(self) -> str | None:
        return read_mode(self.inst.data_dir)

    def mode_fingerprint(self) -> str | None:
        path = self.inst.data_dir / "installation-mode.json"
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except FileNotFoundError:
            return None

    def _hold_data_root(self) -> maintenance.RuntimeLock:
        lock = maintenance.hold_data_root(
            self.inst.data_dir, DATA_ROOT_WAIT,
            waiting=lambda: self.progress("Waiting for another process (e.g. a migration from an interrupted "
                                          "upgrade) to release the data root"))
        self._hand_back(lock.path)  # the lock file stays usable by the data root's own account
        return lock

    def backup_space(self) -> tuple[bool, str]:
        return check_backup_space(self.inst.data_dir)

    def backup(self, dest: Path) -> None:
        lock = self._hold_data_root()
        try:
            self.progress(f"Backing up the data root to {dest}")
            backup_data_root(self.inst.data_dir, dest)
            self._hand_back(dest.parent)
        finally:
            lock.release()

    def restore(self, backup: Path, aside: Path) -> None:
        lock = self._hold_data_root()
        try:
            self.progress(f"Restoring the data root from {backup} (current data kept in {aside})")
            restore_data_root(self.inst.data_dir, backup, aside)
            self._hand_back(self.inst.data_dir)
        finally:
            lock.release()

    def prune_backups(self, protect: tuple[Path, ...]) -> list[str]:
        return prune_backups(self.inst.data_dir, protect=protect)

    def write_marker(self, record: dict, token: str) -> None:
        maintenance.write_upgrade_marker(self.inst.data_dir, record, token)
        self._hand_back(self.inst.data_dir / maintenance.UPGRADE_MARKER)

    def remove_marker(self) -> None:
        (self.inst.data_dir / maintenance.UPGRADE_MARKER).unlink(missing_ok=True)

    def _hand_back(self, path: Path) -> None:
        """Keep data-root files owned by the account that owns the data root."""


class StandaloneToolkit(BaseToolkit):
    edition = "standalone"

    def check_permissions(self) -> list[tuple[str, bool, str]]:
        checks = []
        if hasattr(os, "geteuid"):
            root = os.geteuid() == 0
            checks.append(("not_root", not root, "Run as the user who owns this installation, not root"
                           if root else "running as the installation's user"))
            for path in (self.layout.prefix, self.inst.data_dir):
                owned = path.exists() and path.stat().st_uid == os.geteuid()
                checks.append(("owner", owned, f"{path} is owned by this user" if owned else
                               f"{path} must be owned by the user running the upgrade"))
        return checks

    def is_running(self) -> bool:
        return maintenance.worker_running(self.inst.data_dir)

    def stop(self, timeout: float) -> None:
        if not self.is_running():
            return
        self.progress("Asking the running Worker to finish its current work and stop")
        maintenance.request_stop(self.inst.data_dir, "upgrade")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_running():
                maintenance.withdraw_stop(self.inst.data_dir)
                return
            time.sleep(0.5)
        maintenance.withdraw_stop(self.inst.data_dir)
        raise StopRefused(f"The Worker was still working after {int(timeout)} s. Nothing was changed; "
                          "it continues normally. Try again when the current work has finished.")

    def ensure_stopped(self) -> None:
        # Normally nothing runs here (the upgrade marker blocks startup). After an
        # interrupted upgrade, an orphaned health-check Worker may: stop it the same way.
        if self.is_running():
            self.stop(60)

    def start(self) -> str:
        launcher = self.layout.bin / ("nature-worker.cmd" if sys.platform == "win32" else "nature-worker")
        return f"Start Nature AI Worker again with: {launcher}"

    def health(self, version: str, token: str) -> None:
        """Start the version on a free loopback port with the real data, check it, stop it."""
        import httpx
        port = free_port()
        logs = self.layout.upgrade / "logs"
        logs.mkdir(mode=0o700, exist_ok=True)
        log_path = logs / f"health-{version}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        env = clean_env({
            "NATURE_WORKER_DATA_DIR": str(self.inst.data_dir),
            "NATURE_WORKER_WORKSPACE": str(self.inst.workspace_dir),
            "NATURE_WORKER_HOST": "127.0.0.1", "NATURE_WORKER_PORT": str(port),
            maintenance.TOKEN_ENV: token, "NATURE_WORKER_INSTALL_PREFIX": str(self.layout.prefix),
        })
        self.progress(f"Checking that {version} starts and answers (port {port})")
        with open(log_path, "wb") as log:
            proc = subprocess.Popen([str(self.layout.python(version)), "-I", "-m", "nature_worker"],
                                    env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        try:
            with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
                deadline = time.monotonic() + HEALTH_TIMEOUT
                while True:
                    if proc.poll() is not None:
                        raise ToolkitError(f"{version} exited during startup (exit {proc.returncode}); "
                                           f"see {log_path}")
                    try:
                        health = client.get("/api/health")
                        if health.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline:
                        raise ToolkitError(f"{version} did not answer within {int(HEALTH_TIMEOUT)} s; see {log_path}")
                    time.sleep(0.3)
                if health.json().get("version") != version:
                    raise ToolkitError(f"The started Worker reports {health.json().get('version')!r}, not {version}.")
                page, script = client.get("/"), client.get("/static/app.js")
                settings, conversations = client.get("/api/settings"), client.get("/api/conversations")
                if page.status_code != 200 or "<html" not in page.text.lower() or script.status_code != 200:
                    raise ToolkitError(f"{version} does not serve its web UI.")
                if settings.status_code != 200 or settings.json().get("version") != version:
                    raise ToolkitError(f"{version} cannot read its settings.")
                if conversations.status_code != 200 or page.headers.get("x-nature-worker-version") != version:
                    raise ToolkitError(f"{version} cannot read its work history.")
        finally:
            self._stop_process(proc)

    def _stop_process(self, proc: subprocess.Popen) -> None:
        maintenance.request_stop(self.inst.data_dir, "upgrade health check finished")
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        finally:
            maintenance.withdraw_stop(self.inst.data_dir)


class PlatformClientToolkit(BaseToolkit):
    """Linux systemd Platform Client installed by scripts/platform-client-linux-install.sh."""

    edition = "platform_client"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not sys.platform.startswith("linux") and type(self) is PlatformClientToolkit:
            raise ToolkitError("This Platform Client toolkit is for the Linux systemd service.")
        if not (self.inst.account and self.inst.service and self.inst.port):
            raise ToolkitError("The installation record lacks the Platform Client account, service or port.")
        self.data_user = self.inst.account

    def check_permissions(self) -> list[tuple[str, bool, str]]:
        import pwd
        checks = [("root", os.geteuid() == 0, "running as root" if os.geteuid() == 0 else
                   "Platform Client code is root-owned: run the upgrade with sudo")]
        unsafe = []
        path = self.layout.prefix
        for p in (path, *path.parents):
            info = p.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                unsafe.append(str(p))
        checks.append(("code_root_owned", not unsafe, "code directory chain is root-owned and not writable "
                       "by others" if not unsafe else "writable by non-root: " + ", ".join(unsafe)))
        try:
            uid = pwd.getpwnam(self.inst.account).pw_uid
        except KeyError:
            checks.append(("account", False, f"account {self.inst.account} does not exist"))
            return checks
        info = self.inst.data_dir.stat()
        private = info.st_uid == uid and info.st_mode & 0o077 == 0 and uid != 0
        checks.append(("data_private", private, f"{self.inst.data_dir} is private to {self.inst.account}"
                       if private else f"{self.inst.data_dir} must be owned by {self.inst.account}, mode 0700"))
        return checks

    def _systemctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["systemctl", *args, self.inst.service]
        if check:
            return self.runner.run(cmd, timeout=120)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    def is_running(self) -> bool:
        return self._systemctl("is-active", "--quiet", check=False).returncode == 0

    def stop(self, timeout: float) -> None:
        if self.is_running():
            self.progress("Asking the Platform Client to finish running submissions and sign out")
            maintenance.request_stop(self.inst.data_dir, "upgrade")
            self._hand_back(self.inst.data_dir / maintenance.STOP_REQUEST)
            deadline = time.monotonic() + timeout
            while self.is_running() and time.monotonic() < deadline:
                time.sleep(0.5)
            maintenance.withdraw_stop(self.inst.data_dir)
            if self.is_running():
                raise StopRefused(f"The Platform Client still had a submission running after {int(timeout)} s. "
                                  "Nothing was changed. Try again later.")
        self._systemctl("stop")

    def ensure_stopped(self) -> None:
        self._systemctl("stop")

    def start(self) -> str:
        self._systemctl("start")
        return f"{self.inst.service} restarted. Sign in to the Platform again."

    def health(self, version: str, token: str) -> None:
        import httpx
        # The service starts without the upgrade token; data is migrated and
        # consistent with `version` here, so the marker is lifted for the check.
        self.remove_marker()
        self.progress(f"Starting {self.inst.service} on {version}")
        self._systemctl("restart")
        deadline = time.monotonic() + HEALTH_TIMEOUT
        status = None
        with httpx.Client(base_url=f"http://127.0.0.1:{self.inst.port}", timeout=5, trust_env=False) as client:
            while time.monotonic() < deadline:
                try:
                    status = client.get("/api/status")
                    if status.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if self._systemctl("is-failed", "--quiet", check=False).returncode == 0:
                    break
                time.sleep(0.5)
            if status is None or status.status_code != 200:
                raise ToolkitError(f"{self.inst.service} did not start on {version}; "
                                   f"see journalctl -u {self.inst.service}")
            if status.json().get("version") != version:
                raise ToolkitError(f"The service reports {status.json().get('version')!r}, not {version}.")
            if client.get("/").status_code != 200:
                raise ToolkitError(f"{version} does not serve its web UI.")

    def _hand_back(self, path: Path) -> None:
        import pwd
        entry = pwd.getpwnam(self.inst.account)
        paths = [path]
        if path.is_dir():
            paths += [Path(root) / name for root, dirs, files in os.walk(path) for name in dirs + files]
        for p in paths:
            os.chown(p, entry.pw_uid, entry.pw_gid, follow_symlinks=False)


class WindowsPlatformClientToolkit(PlatformClientToolkit):
    """Windows service installed by ``nature-worker-platform-client-setup install``.

    The upgrader runs elevated because the code is under Program Files. Data-root
    work runs as the administrator; files it creates inherit the data root's DACL
    (SYSTEM, Administrators, the service account), and files it moves are reset
    to that DACL, so the service account keeps exactly its access.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if sys.platform != "win32":
            raise ToolkitError("This Platform Client toolkit is for the Windows service.")
        self.data_user = None

    def check_permissions(self) -> list[tuple[str, bool, str]]:
        from ..windows_security import (WindowsSecurityError, admin_only_problems, current_token,
                                        parent_problems, private_problems, read_acl, service_sid)
        token = current_token()
        admin = token.administrator and token.elevated
        checks = [("administrator", admin, "running as administrator" if admin else
                   "Platform Client code is under Program Files: run the upgrade as administrator")]
        try:
            problems = [f"{p}: {msg}" for p in (self.layout.prefix, self.layout.runtime, self.layout.versions)
                        if p.exists() for msg in admin_only_problems(read_acl(p))]
            problems += [f"{p}: {msg}" for p in self.layout.prefix.parents for msg in parent_problems(read_acl(p))]
        except WindowsSecurityError as exc:
            problems = [str(exc)]
        checks.append(("code_admin_owned", not problems, "code folders can be changed by administrators only"
                       if not problems else "; ".join(problems)))
        try:
            data = private_problems(read_acl(self.inst.data_dir), service_sid(self.inst.service))
        except WindowsSecurityError as exc:
            data = [str(exc)]
        checks.append(("data_private", not data, f"{self.inst.data_dir} is private to {self.inst.account}"
                       if not data else "; ".join(data)))
        return checks

    def _state(self) -> int | None:
        from .. import windows_service
        status = windows_service.query(self.inst.service)
        return status.state if status else None

    def is_running(self) -> bool:
        return self._state() not in (None, 1)

    def _stop_service(self) -> None:
        from .. import windows_service
        try:
            windows_service.stop(self.inst.service, 120)
        except windows_service.ServiceError as exc:
            raise ToolkitError(str(exc)) from exc

    def stop(self, timeout: float) -> None:
        if self.is_running():
            self.progress("Asking the Platform Client to finish running submissions and sign out")
            maintenance.request_stop(self.inst.data_dir, "upgrade")
            deadline = time.monotonic() + timeout
            while self.is_running() and time.monotonic() < deadline:
                time.sleep(0.5)
            maintenance.withdraw_stop(self.inst.data_dir)
            if self.is_running():
                raise StopRefused(f"The Platform Client still had a submission running after {int(timeout)} s. "
                                  "Nothing was changed. Try again later.")
        self._stop_service()

    def ensure_stopped(self) -> None:
        self._stop_service()

    def start(self) -> str:
        from .. import windows_service
        try:
            windows_service.start(self.inst.service, 90)
        except windows_service.ServiceError as exc:
            raise ToolkitError(str(exc)) from exc
        return f"{self.inst.service} restarted. Sign in to the Platform again."

    def health(self, version: str, token: str) -> None:
        import httpx
        from .. import windows_service
        self.remove_marker()
        self.progress(f"Starting {self.inst.service} on {version}")
        self._stop_service()
        log = self.inst.data_dir / "logs" / "service.log"
        try:
            windows_service.start(self.inst.service, HEALTH_TIMEOUT)
        except windows_service.ServiceError as exc:
            raise ToolkitError(f"{self.inst.service} did not start on {version} ({exc}); see {log}") from exc
        status = None
        with httpx.Client(base_url=f"http://127.0.0.1:{self.inst.port}", timeout=5, trust_env=False) as client:
            deadline = time.monotonic() + HEALTH_TIMEOUT
            while time.monotonic() < deadline:
                try:
                    status = client.get("/api/status")
                    if status.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if self._state() in (None, 1):
                    break
                time.sleep(0.5)
            if status is None or status.status_code != 200:
                raise ToolkitError(f"{self.inst.service} did not answer on {version}; see {log}")
            if status.json().get("version") != version:
                raise ToolkitError(f"The service reports {status.json().get('version')!r}, not {version}.")
            if client.get("/").status_code != 200:
                raise ToolkitError(f"{version} does not serve its web UI.")

    def _hand_back(self, path: Path) -> None:
        """Give files the data root's own permissions; the data root's DACL itself is never reset."""
        if path == self.inst.data_dir:
            if any(path.iterdir()):
                self.runner.run(["icacls", str(path / "*"), "/reset", "/T", "/C", "/Q"])
        elif path.exists():
            self.runner.run(["icacls", str(path), "/reset", "/T", "/C", "/Q"])


def toolkit_for(layout: Layout, inst: Installation, **kwargs) -> BaseToolkit:
    if inst.edition == "platform_client":
        if sys.platform == "win32":
            return WindowsPlatformClientToolkit(layout, inst, **kwargs)
        return PlatformClientToolkit(layout, inst, **kwargs)
    return StandaloneToolkit(layout, inst, **kwargs)
