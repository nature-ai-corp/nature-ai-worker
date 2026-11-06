# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Coordinated stop for upgrades, and the guards that keep a Worker stopped.

- ``RuntimeLock``: a running Standalone Worker holds ``worker.lock`` in its
  data root (an OS file lock, released by the OS if the process dies). The
  upgrader uses it to know whether that installation is running, and a second
  Worker can never open the same data root.
- Stop request: the upgrader writes ``maintenance-request.json`` into the data
  root. The Worker then accepts no new work, lets running Agent work (or a
  Platform submission) finish, and exits cleanly. Pending approvals and
  conversations are stored and survive. Withdrawing the request before that
  resumes normal operation.
- Upgrade marker: while ``upgrade-in-progress.json`` exists, the Worker does
  not start, except the upgrader's own health check, which proves it holds
  the marker's token. An interrupted upgrade therefore never lets old code run
  on half-migrated data or new code on unmigrated data.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .installation_mode import ModeError

RUNTIME_LOCK = "worker.lock"
RUNTIME_INFO = "worker-runtime.json"
STOP_REQUEST = "maintenance-request.json"
UPGRADE_MARKER = "upgrade-in-progress.json"
TOKEN_ENV = "NATURE_WORKER_UPGRADE_TOKEN"


class RuntimeLock:
    """Exclusive, non-blocking OS lock on a file; held until ``release``."""

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


class DataRootBusy(ModeError):
    """Another process (a Worker, or a migration) still holds the data root."""


def hold_data_root(data_dir: Path, timeout: float, *,
                   waiting: Callable[[], None] = lambda: None) -> RuntimeLock:
    """Take the data root's runtime lock before changing its data.

    Workers and data migrations hold this lock for as long as they run, and
    the OS releases it when such a process dies. Waiting for it therefore
    guarantees that no earlier process - for example a migration left
    running by an interrupted upgrade - can still write to the data.
    """
    lock = RuntimeLock(data_dir / RUNTIME_LOCK)
    deadline = time.monotonic() + timeout
    announced = False
    while not lock.acquire():
        if time.monotonic() >= deadline:
            raise DataRootBusy(
                f"{data_dir} is still in use by another process (a running Worker, or a data migration "
                f"from an interrupted upgrade) after {int(timeout)} s. Nothing was changed. Wait for it "
                "to finish, then try again.")
        if not announced:
            waiting()
            announced = True
        time.sleep(0.2)
    return lock


def worker_running(data_dir: Path) -> bool:
    """Whether a Worker process holds this data root's runtime lock."""
    if not (data_dir / RUNTIME_LOCK).exists():
        return False
    probe = RuntimeLock(data_dir / RUNTIME_LOCK)
    if probe.acquire():
        probe.release()
        return False
    return True


def replace_file(tmp: Path, path: Path) -> None:
    """``os.replace`` that tolerates Windows' transient sharing violations.

    On Windows, replacing a file fails while another process (a status
    reader, an antivirus scanner) has it open; that clears within moments.
    """
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 39:
                raise
            time.sleep(0.05)


def read_text(path: Path) -> str:
    """Read a small state file; on Windows, retry while it is being replaced."""
    for attempt in range(40):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if sys.platform != "win32" or attempt == 39:
                raise
            time.sleep(0.05)
    raise AssertionError("unreachable")


def _write_json(path: Path, record: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(record, stream)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    replace_file(tmp, path)


def request_stop(data_dir: Path, reason: str) -> None:
    _write_json(data_dir / STOP_REQUEST, {"schema": 1, "reason": reason, "requested_at": time.time()})


def withdraw_stop(data_dir: Path) -> None:
    (data_dir / STOP_REQUEST).unlink(missing_ok=True)


def stop_requested(data_dir: Path) -> bool:
    return (data_dir / STOP_REQUEST).is_file()


def write_runtime_info(data_dir: Path, version: str, port: int) -> None:
    _write_json(data_dir / RUNTIME_INFO, {"pid": os.getpid(), "version": version, "port": port,
                                          "started_at": time.time()})


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def write_upgrade_marker(data_dir: Path, record: dict, token: str) -> None:
    _write_json(data_dir / UPGRADE_MARKER, {**record, "schema": 1, "token_sha256": token_digest(token)})


def read_upgrade_marker(data_dir: Path) -> dict | None:
    path = data_dir / UPGRADE_MARKER
    if path.is_symlink():
        raise ModeError("Upgrade marker must not be a symbolic link.")
    try:
        return json.loads(read_text(path))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ModeError("Unreadable upgrade marker: an upgrade did not finish. "
                        "Run `nature-worker-upgrade recover`.") from exc


def require_no_upgrade_in_progress(data_dir: Path) -> None:
    """Refuse to start while an upgrade owns this data root (see module docs)."""
    marker = read_upgrade_marker(data_dir)
    if marker is None:
        return
    token = os.environ.get(TOKEN_ENV, "")
    if token and hmac.compare_digest(token_digest(token), str(marker.get("token_sha256", ""))):
        return
    raise ModeError(
        f"An upgrade from {marker.get('from_version')} to {marker.get('to_version')} is in progress "
        "or did not finish, so Nature AI Worker will not start on this data. "
        "Run `nature-worker-upgrade status`, then `nature-worker-upgrade recover` if it was interrupted.")


class MaintenanceWatcher(threading.Thread):
    """Polls for a stop request; drains, then calls ``on_ready`` once idle.

    ``busy`` returns how much work is still running. ``on_drain`` switches
    the server between normal and draining (no new work accepted).
    """

    def __init__(self, data_dir: Path, *, busy: Callable[[], int],
                 on_drain: Callable[[bool], None], on_ready: Callable[[], None],
                 interval: float = 0.5):
        super().__init__(name="maintenance-watcher", daemon=True)
        self.data_dir = data_dir
        self.busy, self.on_drain, self.on_ready = busy, on_drain, on_ready
        self.interval = interval
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        draining = False
        while not self._halt.wait(self.interval):
            requested = stop_requested(self.data_dir)
            if requested != draining:
                draining = requested
                self.on_drain(draining)
            if draining and self.busy() == 0:
                self.on_ready()
                return
