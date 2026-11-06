# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Upgrade reliability: no restore while an old migration runs; bounded, private backups."""
import json
from collections import namedtuple
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nature_worker import maintenance, migrations
from nature_worker.upgrade.engine import UpgradeEngine
from nature_worker.upgrade.toolkit import StandaloneToolkit
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from upgrade_fixtures import FakeToolkit, db_schema, make_bundle, managed_install, new_database, trusted

KEY = Ed25519PrivateKey.generate()
DiskUsage = namedtuple("DiskUsage", "total used free")

SRC = str(Path(migrations.__file__).resolve().parents[1])

# A process that holds the data root the way a migration orphaned by an
# interrupted upgrade does, and writes to the database after a delay.
ORPHAN = """
import sqlite3, sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from nature_worker.maintenance import hold_data_root
data = Path(sys.argv[2])
lock = hold_data_root(data, 10)
print("holding", flush=True)
time.sleep(float(sys.argv[3]))
conn = sqlite3.connect(data / "worker.db")
conn.execute("CREATE TABLE orphan_write (v INTEGER)")
conn.execute("PRAGMA user_version = 99")
conn.commit()
conn.close()
"""


def _orphan(data: Path, seconds: float) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-I", "-c", ORPHAN, SRC, str(data), str(seconds)],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "holding"
    return proc


def _tables(data: Path) -> set[str]:
    conn = sqlite3.connect(data / "worker.db")
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


# -- 1. A restore never runs while an old migration can still write -----------------------

def test_restore_waits_for_a_migration_left_running_by_an_interrupted_upgrade(tmp_path):
    layout, inst = managed_install(tmp_path, schema=1)
    backup = migrations.backup_data_root(inst.data_dir, inst.data_dir / "backups" / "upgrade-x")
    orphan = _orphan(inst.data_dir, 3)
    started = time.monotonic()
    StandaloneToolkit(layout, inst).restore(backup, inst.data_dir / "backups" / "aside")
    waited = time.monotonic() - started
    orphan.wait(timeout=30)
    assert orphan.returncode == 0
    # The restore began only after the orphan had finished, so its late write
    # cannot reach the restored data: the data is exactly the backup.
    assert waited >= 2.5, f"restore did not wait for the process holding the data root ({waited:.1f} s)"
    assert db_schema(inst.data_dir) == 1 and "orphan_write" not in _tables(inst.data_dir)
    assert (inst.data_dir / "backups" / "aside" / "worker.db").exists()


def test_restore_refuses_and_changes_nothing_when_the_data_root_stays_busy(tmp_path, monkeypatch):
    layout, inst = managed_install(tmp_path, schema=1)
    backup = migrations.backup_data_root(inst.data_dir, inst.data_dir / "backups" / "upgrade-x")
    monkeypatch.setattr("nature_worker.upgrade.toolkit.DATA_ROOT_WAIT", 0.5)
    orphan = _orphan(inst.data_dir, 5)
    try:
        before = (inst.data_dir / "worker.db").read_bytes()
        with pytest.raises(maintenance.DataRootBusy):
            StandaloneToolkit(layout, inst).restore(backup, inst.data_dir / "backups" / "aside")
        assert (inst.data_dir / "worker.db").read_bytes() == before
        assert not (inst.data_dir / "backups" / "aside").exists()
    finally:
        orphan.wait(timeout=30)


def test_migration_cli_holds_the_data_root_while_it_migrates(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    new_database(data, 0)
    seen = []

    def probe(conn):
        seen.append(maintenance.worker_running(data))

    monkeypatch.setattr(migrations, "MIGRATIONS", [(1, "baseline", lambda c: None), (2, "probe", probe)])
    assert migrations.main(["migrate", "--no-backup", "--data-dir", str(data)]) == 0
    assert seen == [True]  # held during the migration
    assert not maintenance.worker_running(data)  # released afterwards


def test_migration_cli_refuses_a_data_root_in_use(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    new_database(data, 0)
    monkeypatch.setattr(migrations, "MIGRATION_LOCK_WAIT", 0.3)
    worker = maintenance.RuntimeLock(data / maintenance.RUNTIME_LOCK)
    assert worker.acquire()
    try:
        assert migrations.main(["migrate", "--no-backup", "--data-dir", str(data)]) == 1
        assert "still in use" in capsys.readouterr().err
        assert db_schema(data) == 0
    finally:
        worker.release()


# -- 2. Bounded, private backups and a disk-space preflight ------------------------------

def _backup(data: Path, name: str, created: float) -> Path:
    path = data / "backups" / name
    path.mkdir(parents=True)
    (path / "master.key").write_text("k")
    (path / "BACKUP.json").write_text(json.dumps({"schema": 1, "created_at": created}))
    return path


def test_retention_keeps_newest_and_protected_backups_and_never_touches_moved_aside_data(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for i, name in enumerate(["install-a", "upgrade-b", "schema-1-before-2-c", "upgrade-d", "upgrade-e"]):
        _backup(data, name, 1000 + i)
    partial = data / "backups" / "upgrade-partial"
    partial.mkdir()
    (partial / "worker.db").write_text("half")
    for aside in ("failed-upgrade-x-1234", "rolled-back-y-5678"):
        (data / "backups" / aside).mkdir()
        (data / "backups" / aside / "worker.db").write_text("only copy of later work")
    _backup(tmp_path, "outside", 1)  # tmp_path/backups/outside: not part of the data root
    if sys.platform != "win32":
        (data / "backups" / "upgrade-link").symlink_to(tmp_path / "backups" / "outside")
    removed = migrations.prune_backups(data, keep=3, protect=(data / "backups" / "install-a",))
    left = sorted(p.name for p in (data / "backups").iterdir())
    assert removed == ["upgrade-b", "upgrade-partial"]
    assert {"install-a", "schema-1-before-2-c", "upgrade-d", "upgrade-e",
            "failed-upgrade-x-1234", "rolled-back-y-5678"} <= set(left)
    assert (tmp_path / "backups" / "outside" / "master.key").exists()  # a link is never followed


def test_backup_refuses_before_writing_when_the_disk_is_too_full(tmp_path, monkeypatch):
    data = tmp_path / "data"
    new_database(data, 1)
    monkeypatch.setattr(migrations.shutil, "disk_usage",
                        lambda _p: DiskUsage(total=2048, used=1024, free=1024))
    with pytest.raises(migrations.DataSchemaError, match="not enough free disk space"):
        migrations.backup_data_root(data, data / "backups" / "upgrade-x")
    assert not (data / "backups" / "upgrade-x").exists()


def test_backups_directory_is_private(tmp_path):
    data = tmp_path / "data"
    new_database(data, 1)
    (data / "backups").mkdir(mode=0o755)
    migrations.backup_data_root(data, data / "backups" / "upgrade-x")
    if sys.platform != "win32":
        assert (data / "backups").stat().st_mode & 0o077 == 0


@pytest.fixture
def engine(tmp_path):
    layout, inst = managed_install(tmp_path)
    toolkit = FakeToolkit(layout, inst)
    return UpgradeEngine(layout, toolkit, keys=[trusted(KEY)]), toolkit, layout, inst, tmp_path


def test_insufficient_disk_space_is_refused_before_the_worker_is_stopped(engine):
    eng, toolkit, layout, inst, tmp = engine
    toolkit.running = True
    toolkit.space = (False, "not enough free disk space for the data backup: 500 MiB needed, 10 MiB free")
    out = eng.upgrade(str(make_bundle(tmp, KEY, "1.1.0", data_schema=2)), lambda _p: True)
    assert out.outcome == "ineligible" and "not enough free disk space" in out.message
    assert "stop" not in toolkit.calls and toolkit.running and layout.current() == "1.0.0"


def test_successful_upgrade_prunes_older_backups_but_keeps_its_own(engine):
    eng, toolkit, layout, inst, tmp = engine
    for i in range(5):
        _backup(inst.data_dir, f"upgrade-old{i}", 100 + i)
    out = eng.upgrade(str(make_bundle(tmp, KEY, "1.1.0", data_schema=2)), lambda _p: True)
    assert out.outcome == "succeeded", out.message
    own = Path(layout.history()[-1]["backup"])
    kept = sorted(p.name for p in (inst.data_dir / "backups").iterdir())
    assert own.name in kept and len([k for k in kept if k.startswith("upgrade-")]) == 3
    assert "Removed 3 older data backup(s)" in out.message


def test_backup_interrupted_or_failed_leaves_no_partial_copy(engine):
    eng, toolkit, layout, inst, tmp = engine

    original = toolkit.backup

    def half_backup(dest):
        toolkit.calls.append("backup")
        dest.mkdir(parents=True)
        (dest / "worker.db").write_text("half a copy")
        raise OSError("No space left on device")

    toolkit.backup = half_backup
    out = eng.upgrade(str(make_bundle(tmp, KEY, "1.1.0", data_schema=2)), lambda _p: True)
    toolkit.backup = original
    assert out.outcome == "rolled_back" and layout.current() == "1.0.0" and db_schema(inst.data_dir) == 1
    assert not any(p.name.startswith("upgrade-") for p in (inst.data_dir / "backups").iterdir())
