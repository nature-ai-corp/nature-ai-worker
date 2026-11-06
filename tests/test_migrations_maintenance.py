# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Data migrations, backups, the stop handshake and the upgrade marker,
including how the real Standalone and Platform Client apps apply them."""
import os
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from nature_worker import maintenance, migrations
from nature_worker.app import create_app
from nature_worker.config import Config
from nature_worker.installation_mode import ModeError
from upgrade_fixtures import db_schema, new_database


def test_new_database_is_stamped_with_the_newest_schema(tmp_path):
    create_app(Config(data_dir=tmp_path / "data", workspace_dir=tmp_path / "ws"))
    assert db_schema(tmp_path / "data") == migrations.DATA_SCHEMA
    assert not (tmp_path / "data" / "backups").exists()


def test_unversioned_v010_data_is_backed_up_then_migrated(tmp_path):
    data = tmp_path / "data"
    new_database(data, 0, conversations=("legacy",))
    app = create_app(Config(data_dir=data, workspace_dir=tmp_path / "ws", allowed_hosts=["testserver"]))
    assert db_schema(data) == migrations.DATA_SCHEMA
    backups = list((data / "backups").iterdir())
    assert len(backups) == 1 and db_schema(backups[0]) == 0
    if os.name != "nt":  # Windows uses ACLs, not mode bits
        assert oct(backups[0].stat().st_mode & 0o777) == "0o700"
    assert [c["id"] for c in TestClient(app).get("/api/conversations").json()] == ["legacy"]


def test_data_from_a_newer_version_is_never_opened(tmp_path):
    data = tmp_path / "data"
    new_database(data, migrations.DATA_SCHEMA + 1)
    before = (data / "worker.db").read_bytes()
    with pytest.raises(migrations.DataSchemaError, match="newer Nature AI Worker"):
        create_app(Config(data_dir=data, workspace_dir=tmp_path / "ws"))
    assert (data / "worker.db").read_bytes() == before


def test_failing_migration_leaves_the_previous_version(tmp_path):
    data = tmp_path / "data"
    new_database(data, 1)

    def broken(conn):
        conn.execute("CREATE TABLE half (v INTEGER)")
        raise RuntimeError("migration bug")
    steps = migrations.MIGRATIONS + [(2, "adds a table", lambda c: c.execute("CREATE TABLE ok (v INTEGER)")),
                                     (3, "broken", broken)]
    with pytest.raises(RuntimeError):
        migrations.migrate(data, migrations=steps)
    conn = sqlite3.connect(data / "worker.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert db_schema(data) == 2 and "ok" in tables and "half" not in tables


def test_backup_and_restore_keep_secrets_private_and_move_replaced_data_aside(tmp_path):
    data = tmp_path / "data"
    new_database(data, 1)
    (data / "master.key").write_text("secret-key")
    (data / "worker.lock").write_text("")
    (data / "maintenance-request.json").write_text("{}")
    backup = migrations.backup_data_root(data, tmp_path / "b" / "one")
    assert (backup / "master.key").read_text() == "secret-key"
    if os.name != "nt":
        assert oct((backup / "master.key").stat().st_mode & 0o777) == "0o600"
    assert not (backup / "worker.lock").exists() and not (backup / "maintenance-request.json").exists()
    (data / "master.key").write_text("changed")
    migrations.restore_data_root(data, backup, data / "backups" / "aside")
    assert (data / "master.key").read_text() == "secret-key"
    assert (data / "backups" / "aside" / "master.key").read_text() == "changed"
    with pytest.raises(migrations.DataSchemaError):
        migrations.restore_data_root(data, tmp_path, data / "backups" / "aside2")


def test_migrations_cli_reports_schemas(tmp_path, capsys):
    new_database(tmp_path / "d", 0)
    assert migrations.main(["data-schema", "--data-dir", str(tmp_path / "d")]) == 0
    assert '"data_schema": 0' in capsys.readouterr().out
    assert migrations.main(["data-schema", "--edition", "platform_client", "--data-dir", str(tmp_path / "d")]) == 1


def test_runtime_lock_is_exclusive(tmp_path):
    first = maintenance.RuntimeLock(tmp_path / "worker.lock")
    assert first.acquire()
    assert maintenance.worker_running(tmp_path)
    assert not maintenance.RuntimeLock(tmp_path / "worker.lock").acquire()
    first.release()
    assert not maintenance.worker_running(tmp_path)


def test_watcher_drains_waits_for_work_then_stops(tmp_path):
    busy, events = [1], []
    watcher = maintenance.MaintenanceWatcher(tmp_path, busy=lambda: busy[0], on_drain=events.append,
                                             on_ready=lambda: events.append("ready"), interval=0.02)
    watcher.start()
    maintenance.request_stop(tmp_path, "test")
    time.sleep(0.2)
    assert events == [True]  # draining, but work is still running
    busy[0] = 0
    watcher.join(2)
    assert events == [True, "ready"]


def test_watcher_resumes_when_the_request_is_withdrawn(tmp_path):
    events = []
    watcher = maintenance.MaintenanceWatcher(tmp_path, busy=lambda: 1, on_drain=events.append,
                                             on_ready=lambda: events.append("ready"), interval=0.02)
    watcher.start()
    maintenance.request_stop(tmp_path, "test")
    time.sleep(0.15)
    maintenance.withdraw_stop(tmp_path)
    time.sleep(0.15)
    watcher.stop()
    assert events == [True, False]


def test_upgrade_marker_blocks_startup_except_with_its_token(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    maintenance.write_upgrade_marker(data, {"from_version": "1.0.0", "to_version": "1.1.0"}, "s3cret-value")
    config = Config(data_dir=data, workspace_dir=tmp_path / "ws")
    monkeypatch.delenv(maintenance.TOKEN_ENV, raising=False)
    with pytest.raises(ModeError, match="1.0.0 to 1.1.0"):
        create_app(config)
    monkeypatch.setenv(maintenance.TOKEN_ENV, "wrong")
    with pytest.raises(ModeError):
        create_app(config)
    monkeypatch.setenv(maintenance.TOKEN_ENV, "s3cret-value")
    create_app(config)
    assert "s3cret-value" not in (data / maintenance.UPGRADE_MARKER).read_text()


def test_draining_worker_refuses_new_work_and_reports_its_version(tmp_path):
    from nature_worker import __version__
    app = create_app(Config(data_dir=tmp_path / "d", workspace_dir=tmp_path / "w", allowed_hosts=["testserver"]))
    client = TestClient(app)
    conv = client.post("/api/conversations").json()["id"]
    app.state.draining = True
    sent = client.post(f"/api/conversations/{conv}/messages", json={"text": "hi"})
    assert sent.status_code == 503 and "upgrade" in sent.json()["detail"]
    reading = client.get("/api/conversations")
    assert reading.status_code == 200 and reading.headers["x-nature-worker-version"] == __version__
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert client.get("/api/settings").json()["installation"] == {"managed": False}


def test_agent_counts_running_work(tmp_path):
    app = create_app(Config(data_dir=tmp_path / "d", workspace_dir=tmp_path / "w"))
    agent = app.state.agent
    assert agent.active_runs() == 0
    with agent._conversation_lock("c1"):
        assert agent.active_runs() == 1
    assert agent.active_runs() == 0


def test_platform_client_drains_reports_version_and_respects_the_marker(tmp_path):
    from nature_worker import __version__
    from nature_worker.platform_client.server import ClientSettings, create_app as client_app, running_submissions
    from platform_fake import FakePlatform
    settings = ClientSettings(data_dir=tmp_path / "pc", platform_origin="https://platform.example",
                              standalone_data_dir=tmp_path / "sa")
    app = client_app(settings, transport=FakePlatform().transport(), verify_startup=False)
    client = TestClient(app, base_url="http://127.0.0.1")
    status = client.get("/api/status")
    assert status.json()["version"] == __version__ and status.headers["x-nature-worker-version"] == __version__
    app.state.draining = True
    assert client.post("/api/submit", json={"content": "x"}).status_code == 503
    assert running_submissions(app) == 0
    (tmp_path / "pc").mkdir()
    maintenance.write_upgrade_marker(tmp_path / "pc", {"from_version": "a", "to_version": "b"}, "t")
    with pytest.raises(ModeError, match="upgrade"):
        client_app(settings, transport=FakePlatform().transport(), verify_startup=True)


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX process")
def test_real_worker_stops_cleanly_on_request(tmp_path):
    """The real `python -m nature_worker` process drains and exits 0 on a stop request."""
    import subprocess
    import sys

    import httpx
    from test_process import _free_port
    port = _free_port()
    env = {**os.environ, "NATURE_WORKER_DATA_DIR": str(tmp_path / "d"),
           "NATURE_WORKER_WORKSPACE": str(tmp_path / "w"), "NATURE_WORKER_PORT": str(port)}
    proc = subprocess.Popen([sys.executable, "-m", "nature_worker"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        second = subprocess.run([sys.executable, "-m", "nature_worker", "--port", str(_free_port())], env=env,
                                capture_output=True, text=True, timeout=30)
        assert second.returncode == 1 and "already using" in second.stderr
        maintenance.request_stop(tmp_path / "d", "test")
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not maintenance.worker_running(tmp_path / "d")
