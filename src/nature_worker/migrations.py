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
from .maintenance import replace_file

DB_FILE = "worker.db"
BACKUPS_DIR = "backups"
PLATFORM_CLIENT_SCHEMA = 1

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


def backup_data_root(data_dir: Path, dest: Path) -> Path:
    """Copy the data root into ``dest`` (which must not exist yet).

    The database is copied with SQLite's online backup, so the copy is
    consistent even if the WAL has not been checkpointed. Everything else
    (keys, the mode marker, ...) is copied as is, private to this account.
    """
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
        backup_data_root(data_dir, data_dir / BACKUPS_DIR /
                         f"schema-{current}-before-{newest}-{time.strftime('%Y%m%d-%H%M%S')}")
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
        applied = [] if args.edition == PLATFORM_CLIENT else migrate(
            args.data_dir, backup=not args.no_backup)
        print(json.dumps({"applied": applied, "data_schema": _edition_schema(args.edition, args.data_dir)}))
        return 0
    except (ModeError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
