# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Signed test bundles and a scripted toolkit for upgrade engine tests.

``FakeToolkit`` replaces only the OS operations (pip, processes, services);
the engine, layout, journal, bundle verification, data backup and restore
are real. Real installations and processes are exercised by
scripts/upgrade_e2e.py, not here.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nature_worker.migrations import backup_data_root, restore_data_root
from nature_worker.upgrade.layout import INSTALLED, Installation, Layout
from nature_worker.upgrade.release import TrustedKey, key_id_for, sign_manifest
from nature_worker.upgrade.toolkit import StopRefused, ToolkitError, host_target

TARGET = host_target()


def trusted(key: Ed25519PrivateKey, scope: str = "local") -> TrustedKey:
    from cryptography.hazmat.primitives import serialization
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return TrustedKey(key_id_for(raw), key.public_key(), scope, "test")


def make_bundle(root: Path, key: Ed25519PrivateKey, version: str, *, data_schema: int = 1,
                min_from: str = "0.0.1", editions=("standalone", "platform_client"),
                target: dict | None = None, client_interface: str = "1.3", notes: str = "Notes") -> Path:
    bundle = root / f"bundle-{version}-{len(list(root.glob('bundle-*')))}"
    (bundle / "wheels").mkdir(parents=True)
    wheels = [f"nature_ai_worker-{version}-py3-none-any.whl", "dep-1.0-py3-none-any.whl"]
    for name in wheels:
        (bundle / "wheels" / name).write_bytes(hashlib.sha256(name.encode()).digest() * 10)
    (bundle / "RELEASE_NOTES.md").write_text(notes)

    def entry(rel: str) -> dict:
        data = (bundle / rel).read_bytes()
        return {"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

    manifest = {
        "schema": 1, "product": "nature-ai-worker", "version": version, "released_at": "2026-10-26",
        "channel": "stable", "target": target or TARGET, "editions": list(editions),
        "upgrade_from": {"min": min_from},
        "data_schema": {"standalone": data_schema, "platform_client": 1},
        "client_interface": client_interface, "notes": entry("RELEASE_NOTES.md"),
        "product_wheel": f"wheels/{wheels[0]}", "artifacts": [entry(f"wheels/{w}") for w in wheels],
        "source": {"repository": "test", "commit": "0" * 40, "tag": f"v{version}"},
    }
    raw = json.dumps(manifest).encode()
    (bundle / "release.json").write_bytes(raw)
    (bundle / "release.json.sig").write_bytes(sign_manifest(raw, key))
    return bundle


def new_database(data_dir: Path, schema: int, conversations=("one",)) -> None:
    from nature_worker.db import SCHEMA
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / "worker.db")
    conn.executescript(SCHEMA)
    for c in conversations:
        conn.execute("INSERT INTO conversations VALUES (?, ?, 1, 1)", (c, c))
    conn.execute(f"PRAGMA user_version = {schema}")
    conn.commit()
    conn.close()
    (data_dir / "installation-mode.json").write_text('{"schema": 1, "mode": "standalone"}\n')


def db_schema(data_dir: Path) -> int:
    conn = sqlite3.connect(data_dir / "worker.db")
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def managed_install(tmp: Path, version: str = "1.0.0", schema: int = 1) -> tuple[Layout, Installation]:
    layout = Layout(tmp / "prefix")
    inst = Installation("standalone", tmp / "data", tmp / "workspace")
    (tmp / "prefix").mkdir()
    layout.write_installation(inst)
    install_fake_version(layout, version, schema)
    layout.set_current(version)
    new_database(inst.data_dir, schema)
    return layout, inst


def install_fake_version(layout: Layout, version: str, schema: int, digest: str = "x") -> None:
    d = layout.version_dir(version)
    (d / "venv").mkdir(parents=True)
    (d / INSTALLED).write_text(json.dumps({"version": version, "manifest_sha256": digest, "code_schema": schema}))


class FakeToolkit:
    """Scripted OS operations; ``fail`` maps a step name to the exception it raises."""

    edition = "standalone"

    def __init__(self, layout: Layout, inst: Installation, *, running: bool = False):
        self.layout, self.inst = layout, inst
        self.running = running
        self.fail: dict[str, Exception] = {}
        self.calls: list[str] = []
        self.health_tokens: list[str] = []
        self.started = 0

    def _step(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise self.fail.pop(name)

    def host_target(self):
        return TARGET

    def check_permissions(self):
        return [("owner", True, "test")]

    def install_version(self, release):
        self._step("install")
        version = str(release.manifest.version)
        install_fake_version(self.layout, version, release.manifest.data_schema["standalone"],
                             release.manifest.digest)

    def remove_incomplete(self, version):
        self.calls.append("remove_incomplete")

    def code_schema(self, version):
        return json.loads((self.layout.version_dir(version) / INSTALLED).read_text())["code_schema"]

    def data_schema(self, version):
        return db_schema(self.inst.data_dir)

    def client_interface(self, version):
        return "1.3"

    def migrate(self, version):
        self._step("migrate")
        conn = sqlite3.connect(self.inst.data_dir / "worker.db")
        conn.execute(f"PRAGMA user_version = {self.code_schema(version)}")
        conn.execute("CREATE TABLE IF NOT EXISTS migrated (v INTEGER)")
        conn.commit()
        conn.close()
        self._step("after_migrate")

    def health(self, version, token):
        self._step(f"health:{version}")
        self.health_tokens.append(token)

    def is_running(self):
        return self.running

    def stop(self, timeout):
        self._step("stop")
        self.running = False

    def ensure_stopped(self):
        self.calls.append("ensure_stopped")

    def start(self):
        self.started += 1
        self.running = True
        return "started"

    def data_mode(self):
        from nature_worker.installation_mode import read_mode
        return read_mode(self.inst.data_dir)

    def mode_fingerprint(self):
        p = self.inst.data_dir / "installation-mode.json"
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None

    def backup(self, dest):
        self._step("backup")
        backup_data_root(self.inst.data_dir, dest)

    def restore(self, backup, aside):
        self._step("restore")
        restore_data_root(self.inst.data_dir, backup, aside)

    def write_marker(self, record, token):
        from nature_worker import maintenance
        maintenance.write_upgrade_marker(self.inst.data_dir, record, token)

    def remove_marker(self):
        (self.inst.data_dir / "upgrade-in-progress.json").unlink(missing_ok=True)


__all__ = ["FakeToolkit", "StopRefused", "ToolkitError", "make_bundle", "managed_install", "trusted",
           "db_schema", "new_database", "install_fake_version"]
