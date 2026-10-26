# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Versioned data migrations for an installation's data root.

Standalone: the data schema is SQLite's ``user_version`` of ``worker.db``.
Version 0 is the unversioned v0.1.0 database; migration 1 only records it as
the baseline. ``db.SCHEMA`` always describes the newest schema, so a new
database is stamped with ``DATA_SCHEMA`` directly.

Each migration runs in one SQLite transaction together with its version bump:
a failing migration leaves the previous version intact. Before migrating,
``migrate`` keeps a copy of the data root in ``backups/`` (the upgrader takes
its own copy and passes ``backup=False``). Code never opens data written by a
newer schema.

Platform Client: the data root holds only the installation-mode marker,
which is schema 1. There are no Platform Client data migrations yet.

``python -I -m nature_worker.migrations code-schema|data-schema|migrate``
is how the upgrader asks a specific installed version about its schema.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Callable

from .installation_mode import PLATFORM_CLIENT, STANDALONE, ModeError, read_mode
from .maintenance import hold_data_root, replace_file

DB_FILE = "worker.db"
BACKUPS_DIR = "backups"
PLATFORM_CLIENT_SCHEMA = 1

# Backups are complete copies of the data root (including the master key and
# the encrypted API keys), so they are kept private and bounded: the newest
# KEEP_BACKUPS copies plus any the caller still needs (for a rollback).
# Data moved aside by a rollback ("failed-upgrade-*", "rolled-back-*") is the
# only copy of that work and is never removed automatically.
KEEP_BACKUPS = 3
PRUNABLE = ("upgrade-", "install-", "schema-")
SPACE_MARGIN = 64 * 1024 * 1024
MIGRATION_LOCK_WAIT = 10.0

Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]

MIGRATIONS: list[Migration] = [
    (1, "Record the v0.1.0 tables as data schema 1", lambda conn: None),
]
DATA_SCHEMA = MIGRATIONS[-1][0]

# Never copied into a backup or restored from one: they describe a running
# process or an upgrade in progress, not user data.
TRANSIENT = frozenset({
    BACKUPS_DIR, "worker.lock", "worker-runtime.json", "maintenance-request.json",
    "upgrade-in-progress.json", DB_FILE + "-wal", DB_FILE + "-shm",
})


class DataSchemaError(ModeError):
    """The data root cannot be used by this code version."""


def data_schema(data_dir: Path) -> int | None:
    """Schema of the Standalone database, or None when there is none yet."""
    path = data_dir / DB_FILE
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def check_supported(data_dir: Path, supported: int = DATA_SCHEMA) -> int | None:
    version = data_schema(data_dir)
    if version is not None and version > supported:
        raise DataSchemaError(
            f"This data was written by a newer Nature AI Worker (data schema {version}; "
            f"this version supports up to {supported}). It was not opened or changed. "
            "Start the newer version, or restore the backup made before that upgrade "
            "(see `nature-worker-upgrade history`).")
    return version


def stamp_new_database(data_dir: Path) -> None:
    """Record a database just created from ``db.SCHEMA`` as the newest schema."""
    conn = sqlite3.connect(data_dir / DB_FILE)
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            conn.execute(f"PRAGMA user_version = {DATA_SCHEMA}")
            conn.commit()
    finally:
        conn.close()


def _copy_private(src: Path, dest: Path) -> None:
    if src.is_dir():
        dest.mkdir(mode=0o700)
        for child in sorted(src.iterdir()):
            _copy_private(child, dest / child.name)
    else:
        shutil.copy2(src, dest, follow_symlinks=False)
        if not dest.is_symlink():
            os.chmod(dest, 0o600)


def _size(path: Path) -> int:
    if path.is_symlink():
        return 0
    if path.is_dir():
        return sum(_size(child) for child in path.iterdir())
    return path.stat().st_size


def backup_size(data_dir: Path) -> int:
    """Bytes a backup of this data root needs (the database counts with its WAL)."""
    return sum(_size(entry) for entry in data_dir.iterdir()
               if entry.name not in TRANSIENT - {DB_FILE + "-wal"} and not entry.name.startswith("."))


def check_backup_space(data_dir: Path) -> tuple[bool, str]:
    need = int(backup_size(data_dir) * 1.1) + SPACE_MARGIN
    free = shutil.disk_usage(data_dir).free
    mib = lambda n: f"{n / 1024 / 1024:.0f} MiB"  # noqa: E731
    if free < need:
        return False, (f"not enough free disk space for the data backup: {mib(need)} needed, "
                       f"{mib(free)} free on the disk holding {data_dir}")
    return True, f"{mib(free)} free; the data backup needs about {mib(need)}"


def backup_data_root(data_dir: Path, dest: Path) -> Path:
    """Copy the data root into ``dest`` (which must not exist yet).

    The database is copied with SQLite's online backup, so the copy is
    consistent even if the WAL has not been checkpointed. Everything else
    (keys, the mode marker, ...) is copied as is, private to this account.
    Refuses, before writing anything, when the disk cannot hold the copy.
    """
    ok, detail = check_backup_space(data_dir)
    if not ok:
        raise DataSchemaError(f"No backup was made: {detail}. Nothing was changed.")
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not dest.parent.is_symlink():
        os.chmod(dest.parent, 0o700)
    dest.mkdir(mode=0o700)
    for entry in sorted(data_dir.iterdir()):
        if entry.name in TRANSIENT or entry.name.startswith("."):
            continue
        if entry.name == DB_FILE:
            source = sqlite3.connect(f"file:{entry}?mode=ro", uri=True)
            target = sqlite3.connect(dest / DB_FILE)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            os.chmod(dest / DB_FILE, 0o600)
        else:
            _copy_private(entry, dest / entry.name)
    (dest / "BACKUP.json").write_text(json.dumps({
        "schema": 1, "data_schema": data_schema(dest) if (dest / DB_FILE).exists() else None,
        "created_at": time.time(),
    }) + "\n", encoding="utf-8")
    return dest


def prune_backups(data_dir: Path, *, keep: int = KEEP_BACKUPS, protect: tuple[Path, ...] = ()) -> list[str]:
    """Remove all but the newest ``keep`` complete backups, never one in ``protect``.

    Also removes incomplete copies (no BACKUP.json) left by an interrupted
    backup. Only call this when no upgrade or migration is in progress.
    Moved-aside data and anything that is not a plain backup directory stay.
    """
    root = data_dir / BACKUPS_DIR
    if root.is_symlink() or not root.is_dir():
        return []
    protected = {Path(p).name for p in protect if p}
    complete, removed = [], []
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_dir() or not entry.name.startswith(PRUNABLE):
            continue
        marker = entry / "BACKUP.json"
        if not marker.is_file():
            if entry.name not in protected:
                shutil.rmtree(entry)
                removed.append(entry.name)
            continue
        try:
            created = float(json.loads(marker.read_text(encoding="utf-8")).get("created_at", 0))
        except (OSError, ValueError, TypeError):
            created = 0.0
        complete.append((created, entry))
    complete.sort(reverse=True)
    for _created, entry in complete[keep:]:
        if entry.name not in protected:
            shutil.rmtree(entry)
            removed.append(entry.name)
    return sorted(removed)


def restore_data_root(data_dir: Path, backup: Path, aside: Path) -> None:
    """Replace the data root with ``backup``. Nothing is deleted: the data
    being replaced is moved into ``aside`` first."""
    if not (backup / "BACKUP.json").is_file():
        raise DataSchemaError(f"{backup} is not a complete data backup.")
    aside.mkdir(mode=0o700, parents=True)
    for entry in sorted(data_dir.iterdir()):
        if entry.name in (BACKUPS_DIR, "upgrade-in-progress.json", "worker.lock"):
            continue
        replace_file(entry, aside / entry.name)
    for entry in sorted(backup.iterdir()):
        if entry.name != "BACKUP.json":
            _copy_private(entry, data_dir / entry.name)


def migrate(data_dir: Path, *, backup: bool = True,
            migrations: list[Migration] | None = None) -> list[int]:
    """Bring an existing Standalone database to the newest schema.

    Returns the versions applied. Refuses data newer than this code. A new
    database (no ``worker.db``) is left to ``Database`` and ``stamp_new_database``.
    """
    migrations = MIGRATIONS if migrations is None else migrations
    newest = migrations[-1][0]
    current = check_supported(data_dir, newest)
    if current is None or current == newest:
        return []
    pending = [m for m in migrations if m[0] > current]
    if backup:
        made = backup_data_root(data_dir, data_dir / BACKUPS_DIR /
                                f"schema-{current}-before-{newest}-{time.strftime('%Y%m%d-%H%M%S')}")
        prune_backups(data_dir, protect=(made,))
    applied = []
    conn = sqlite3.connect(data_dir / DB_FILE, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for version, _description, apply in pending:
            conn.execute("BEGIN IMMEDIATE")
            try:
                apply(conn)
                conn.execute(f"PRAGMA user_version = {int(version)}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            applied.append(version)
    finally:
        conn.close()
    return applied


def _edition_schema(edition: str, data_dir: Path) -> int | None:
    mode = read_mode(data_dir)
    if edition == PLATFORM_CLIENT:
        if mode != PLATFORM_CLIENT:
            raise DataSchemaError("The data root is not a Platform Client installation.")
        return PLATFORM_CLIENT_SCHEMA
    if mode not in (None, STANDALONE):
        raise DataSchemaError("The data root is not a Standalone installation.")
    return data_schema(data_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m nature_worker.migrations")
    parser.add_argument("command", choices=("code-schema", "data-schema", "migrate"))
    parser.add_argument("--edition", choices=(STANDALONE, PLATFORM_CLIENT), default=STANDALONE)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args(argv)
    code = PLATFORM_CLIENT_SCHEMA if args.edition == PLATFORM_CLIENT else DATA_SCHEMA
    if args.command == "code-schema":
        print(json.dumps({"code_schema": code}))
        return 0
    if args.data_dir is None:
        parser.error("--data-dir is required")
    try:
        current = _edition_schema(args.edition, args.data_dir)
        if args.command == "data-schema":
            print(json.dumps({"data_schema": current}))
            return 0
        # Hold the data root for the whole migration: anything that restores
        # or backs up this data root waits until this process has ended.
        lock = hold_data_root(args.data_dir, MIGRATION_LOCK_WAIT)
        try:
            applied = [] if args.edition == PLATFORM_CLIENT else migrate(
                args.data_dir, backup=not args.no_backup)
        finally:
            lock.release()
        print(json.dumps({"applied": applied, "data_schema": _edition_schema(args.edition, args.data_dir)}))
        return 0
    except (ModeError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
