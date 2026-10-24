"""Persistent installation-mode guard for a Standalone Worker installation.

This is a local fail-closed mode marker, NOT a security boundary. An enterprise
release must add an OS-enforced separation, installer residue checks and a
Platform Client runtime before allowing S-C activation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

MODE_FILE = "installation-mode.json"
STANDALONE = "standalone"
PLATFORM_CLIENT = "platform_client"


class ModeError(RuntimeError):
    """The requested runtime violates the installation mode."""


def read_mode(data_dir: Path) -> str | None:
    """Read the persisted mode; reject corrupt or unknown manifests."""
    path = data_dir / MODE_FILE
    if path.is_symlink():
        raise ModeError("Installation mode marker must not be a symbolic link.")
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ModeError("Cannot read installation mode marker.") from exc
    try:
        record = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ModeError("Corrupted installation mode marker.") from exc
    if not isinstance(record, dict) or record.get("schema") != 1:
        raise ModeError("Unrecognized installation mode manifest.")
    mode = record.get("mode")
    if mode not in (STANDALONE, PLATFORM_CLIENT):
        raise ModeError("Unknown installation mode.")
    return mode


def require_standalone(data_dir: Path) -> None:
    """Deny local-Agent startup when this installation is S-C or invalid."""
    mode = read_mode(data_dir)
    if mode == PLATFORM_CLIENT:
        raise ModeError(
            "Platform Client installation cannot run Standalone. "
            "Uninstall, clear enterprise data, verify clean state, then reinstall."
        )


def initialize_standalone(data_dir: Path) -> None:
    """Atomically create a local mode marker after validating an installation.

    Existing v0.1 data without a marker remains standalone-compatible. This
    helper never overwrites a marker; upgrades need a separate gated procedure.
    """
    require_standalone(data_dir)
    path = data_dir / MODE_FILE
    if path.exists() or path.is_symlink():
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"schema": 1, "mode": STANDALONE}) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        require_standalone(data_dir)
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
