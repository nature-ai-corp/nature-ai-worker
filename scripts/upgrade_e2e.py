# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Real upgrade tests on a dedicated, disposable Standalone installation.

    python scripts/upgrade_e2e.py --workdir NEW_DIR [--src SOURCE_TREE]

Builds signed bundles of test versions 9.0.0 .. 9.4.0 from the source
(default: the committed HEAD), creates a managed installation under
NEW_DIR/prefix with its own data root and workspace, and drives real
processes through: install over existing unversioned data, upgrade while
Agent work is running, a drain timeout, refused bundles, an upgrade that
fails and rolls back, an upgrade killed during migration and recovered, an
upgrade killed during verification and recovered, and a user rollback.

Only NEW_DIR is used; no other installation, data root or service is touched.
Writes NEW_DIR/upgrade-e2e-report.md; exits 0 only if every check passed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import httpx  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from nature_worker.upgrade.release import public_key_entry, sign_manifest  # noqa: E402

WIN = sys.platform == "win32"
SECRET = "sk-e2e-" + hashlib.sha256(os.urandom(16)).hexdigest()[:24]  # a stored API key that must survive
BASELINE = '    (1, "Record the v0.1.0 tables as data schema 1", lambda conn: None),\n]'
SLOW = ('lambda conn: (__import__("time").sleep(float(__import__("os").environ.get("E2E_MIGRATION_SLEEP", "0"))), '
        'conn.execute("CREATE TABLE e2e_{n} (v INTEGER)"))')


class Check(Exception):
    pass


def expect(condition, message: str) -> None:
    if not condition:
        raise Check(message)


class FakeModel:
    """OpenAI-compatible chat endpoint answering from a queue, with a delay.

    With ``gate`` set, a reply is held until that file exists (the upgrader's
    stop request) and then for ``delay`` seconds: the Agent run is provably
    still in progress when the stop is requested, however long installing
    the new version took.
    """

    def __init__(self):
        self.queue: list[dict] = []
        self.delay = 0.0
        self.gate: Path | None = None
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self._send({"data": [{"id": "fake"}]})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length") or 0))
                deadline = time.monotonic() + 600
                while model.gate is not None and not model.gate.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                time.sleep(model.delay)
                message = model.queue.pop(0) if model.queue else {"role": "assistant", "content": "ok"}
                self._send({"id": "x", "object": "chat.completion", "created": 0, "model": "fake",
                            "choices": [{"index": 0, "message": message,
                                         "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}]})

            def _send(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


class E2E:
    def __init__(self, workdir: Path, src: Path | None):
        self.w = workdir
        self.prefix, self.data, self.ws = workdir / "prefix", workdir / "data", workdir / "workspace"
        self.src = src
        self.results: list[tuple[str, bool, list[str]]] = []
        self.evidence: list[str] = []
        self.model = FakeModel()
        self.worker: subprocess.Popen | None = None
        self.port = 0

    # -- helpers ------------------------------------------------------------------

    def note(self, text: str) -> None:
        self.evidence.append(text)

    def run(self, cmd, *, env=None, timeout=900, stdin=subprocess.DEVNULL) -> subprocess.CompletedProcess:
        result = subprocess.run([str(c) for c in cmd], env=env, capture_output=True, text=True,
                                timeout=timeout, stdin=stdin)
        (self.w / "logs").mkdir(exist_ok=True)
        with open(self.w / "logs" / "commands.log", "a", encoding="utf-8") as log:
            log.write(f"$ {' '.join(map(str, cmd))}\n[exit {result.returncode}]\n{result.stdout}{result.stderr}\n")
        return result

    def launcher(self, name: str) -> Path:
        return self.prefix / "bin" / (name + (".cmd" if WIN else ""))

    def upgrader(self, *args, env=None, stdin=subprocess.DEVNULL) -> subprocess.CompletedProcess:
        return self.run([self.launcher("nature-worker-upgrade"), *args], env=env, stdin=stdin)

    def python(self, version: str) -> Path:
        venv = self.prefix / "versions" / version / "venv"
        return venv / ("Scripts/python.exe" if WIN else "bin/python")

    def current(self) -> str:
        return (self.prefix / "current").read_text().strip()

    def schema(self, db: Path | None = None) -> int:
        conn = sqlite3.connect(f"file:{db or self.data / 'worker.db'}?mode=ro", uri=True)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def tables(self, db: Path | None = None) -> set[str]:
        conn = sqlite3.connect(f"file:{db or self.data / 'worker.db'}?mode=ro", uri=True)
        try:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()

    def history(self) -> list[dict]:
        path = self.prefix / "upgrade" / "history.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def state(self) -> dict | None:
        path = self.prefix / "upgrade" / "state.json"
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, PermissionError, ValueError):
            return None  # absent, or being replaced at this moment (Windows)

    def free_port(self) -> int:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def start_worker(self, expect_version: str | None = None) -> None:
        self.port = self.free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("NATURE_WORKER_", "PYTHON"))}
        env["NATURE_WORKER_PORT"] = str(self.port)
        log = open(self.w / "logs" / f"worker-{time.time():.0f}.log", "wb")
        self.worker = subprocess.Popen([str(self.launcher("nature-worker"))], env=env, stdout=log,
                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        for _ in range(200):
            if self.worker.poll() is not None:
                raise Check(f"Worker exited at startup (exit {self.worker.returncode})")
            try:
                health = httpx.get(self.base + "/api/health", timeout=1, trust_env=False)
                if health.status_code == 200:
                    if expect_version:
                        expect(health.json()["version"] == expect_version,
                               f"Worker runs {health.json()['version']}, expected {expect_version}")
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise Check("Worker did not answer /api/health")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def api(self, method: str, path: str, **kwargs) -> httpx.Response:
        return httpx.request(method, self.base + path, timeout=120, trust_env=False, **kwargs)

    def stop_worker(self) -> None:
        if self.worker is None or self.worker.poll() is not None:
            self.worker = None
            return
        (self.data / "maintenance-request.json").write_text('{"schema": 1, "reason": "e2e"}')
        try:
            self.worker.wait(timeout=30)
        finally:
            (self.data / "maintenance-request.json").unlink(missing_ok=True)
            if self.worker.poll() is None:
                self.worker.kill()
            self.worker = None

    def worker_refuses_to_start(self) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("NATURE_WORKER_", "PYTHON"))}
        env["NATURE_WORKER_PORT"] = str(self.free_port())
        result = self.run([self.launcher("nature-worker")], env=env, timeout=60)
        expect(result.returncode != 0, "Worker started although an upgrade was unfinished")
        return (result.stdout + result.stderr).strip().splitlines()[-1]

    # -- building test releases -----------------------------------------------------------

    def source_tree(self) -> Path:
        base = self.w / "source"
        if self.src:
            shutil.copytree(self.src, base, ignore=shutil.ignore_patterns(
                ".git", ".venv", "data", "workspace", "build", "dist", "*.egg-info", "__pycache__", ".pytest_cache"))
            self.note(f"Source: working tree {self.src}")
        else:
            archive = self.w / "source.tar"
            with open(archive, "wb") as stream:
                subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", "HEAD"], check=True, stdout=stream)
            with tarfile.open(archive) as tar:
                tar.extractall(base, filter="data")
            commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                                    text=True, check=True).stdout.strip()
            self.note(f"Source: committed HEAD {commit}")
        return base

    def variant(self, version: str, migrations: tuple[int, ...] = (), broken: bool = False) -> Path:
        dest = self.w / f"src-{version}"
        shutil.copytree(self.base_src, dest)
        init = dest / "src" / "nature_worker" / "__init__.py"
        text = init.read_text()
        old = text.split('__version__ = "')[1].split('"')[0]
        init.write_text(text.replace(f'__version__ = "{old}"', f'__version__ = "{version}"'))
        pyproject = dest / "pyproject.toml"
        pyproject.write_text(pyproject.read_text().replace(f'version = "{old}"', f'version = "{version}"', 1))
        if migrations:
            path = dest / "src" / "nature_worker" / "migrations.py"
            text = path.read_text()
            expect(BASELINE in text, "migrations.py baseline entry not found")
            extra = "".join(f'    ({n}, "e2e test migration {n}", {SLOW.format(n=n)}),\n' for n in migrations)
            path.write_text(text.replace(BASELINE, BASELINE[:-1] + extra + "]"))
        if broken:
            path = dest / "src" / "nature_worker" / "__main__.py"
            text = path.read_text()
            path.write_text(text.replace("def main() -> None:\n",
                                         "def main() -> None:\n    raise SystemExit('e2e: broken on purpose')\n", 1))
        return dest

    def bundle(self, version: str, *, min_from: str = "9.0.0", **variant) -> Path:
        out = self.w / "bundles" / version
        notes = self.w / f"notes-{version}.md"
        notes.write_text(f"# Nature AI Worker {version} (e2e test build)\n\nTest release, not for distribution.\n")
        result = self.run([sys.executable, REPO / "scripts" / "release_bundle.py", "build", "--private-key",
                           self.key_file, "--notes", notes, "--out", out, "--src", self.variant(version, **variant),
                           "--min-upgrade-from", min_from])
        expect(result.returncode == 0, f"bundle {version} failed: {result.stderr[-500:]}")
        return out

    def resigned(self, src: Path, name: str, key: Ed25519PrivateKey, edit=None) -> Path:
        dest = self.w / "bundles" / name
        shutil.copytree(src, dest)
        raw = (dest / "release.json").read_bytes()
        if edit:
            record = json.loads(raw)
            edit(record)
            raw = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
            (dest / "release.json").write_bytes(raw)
        (dest / "release.json.sig").write_bytes(sign_manifest(raw, key))
        return dest

    # -- scenarios -------------------------------------------------------------------------

    def scenario(self, name: str, fn) -> None:
        self.evidence = []
        started = time.monotonic()
        try:
            fn()
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            self.stop_worker()
            self.note(f"FAILED: {exc}")
            self.note("```\n" + traceback.format_exc()[-2000:] + "```")
        self.note(f"({time.monotonic() - started:.1f} s)")
        self.results.append((name, ok, self.evidence))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}", flush=True)

    def setup(self) -> None:
        self.w.mkdir(parents=True)
        (self.w / "logs").mkdir()
        self.base_src = self.source_tree()
        key = Ed25519PrivateKey.generate()
        from cryptography.hazmat.primitives import serialization
        self.key_file = self.w / "test-release-key.pem"
        self.key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
        self.key = key
        self.trust = self.w / "trusted-keys.json"
        self.trust.write_text(json.dumps({"schema": 1, "keys": [public_key_entry(key, "e2e test key")]}))
        self.bundles = {
            "A": self.bundle("9.0.0"),
            "B": self.bundle("9.1.0", migrations=(2,)),
            "C": self.bundle("9.2.0", migrations=(2, 3), broken=True),
            "D": self.bundle("9.3.0", migrations=(2,), min_from="9.2.0"),
            "E": self.bundle("9.4.0", migrations=(2, 3)),
        }
        untrusted = Ed25519PrivateKey.generate()
        self.bundles["untrusted"] = self.resigned(self.bundles["E"], "untrusted", untrusted)
        self.bundles["wrong-platform"] = self.resigned(
            self.bundles["E"], "wrong-platform", key, lambda m: m["target"].update(python="3.99"))
        altered = self.w / "bundles" / "altered-manifest"
        shutil.copytree(self.bundles["E"], altered)
        (altered / "release.json").write_bytes((altered / "release.json").read_bytes().replace(b'"9.4.0"', b'"9.4.1"'))
        self.bundles["altered-manifest"] = altered
        tampered = self.w / "bundles" / "tampered-wheel"
        shutil.copytree(self.bundles["E"], tampered)
        wheel = next((tampered / "wheels").glob("nature_ai_worker-*.whl"))
        content = bytearray(wheel.read_bytes())
        content[len(content) // 2] ^= 0xFF  # same size, different content
        wheel.write_bytes(bytes(content))
        self.bundles["tampered-wheel"] = tampered

    def s_install_over_existing_data(self) -> None:
        # An existing v0.1-style data root: unversioned database (user_version 0) with history.
        from nature_worker.db import SCHEMA
        self.data.mkdir(mode=0o700)
        conn = sqlite3.connect(self.data / "worker.db")
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO conversations VALUES ('legacy', 'Legacy work', 1, 1)")
        conn.commit()
        conn.close()
        self.ws.mkdir()
        (self.ws / "keep.txt").write_text("user file\n")
        bootstrap = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                     "from nature_worker.upgrade.__main__ import main; sys.exit(main())")
        result = self.run([sys.executable, "-I", "-c", bootstrap, self.w / "src-9.0.0" / "src", "install",
                           "--bundle", self.bundles["A"], "--prefix", self.prefix, "--data-dir", self.data,
                           "--workspace", self.ws, "--trust-key", self.trust, "--confirm-version", "9.0.0"])
        expect(result.returncode == 0, f"install failed: {result.stdout[-800:]}")
        expect(self.current() == "9.0.0", "9.0.0 not active")
        expect(self.schema() == 1, f"schema {self.schema()} after install, expected 1")
        backups = list((self.data / "backups").glob("install-*"))
        expect(len(backups) == 1 and self.schema(backups[0] / "worker.db") == 0, "no pre-install backup of schema 0")
        self.mode_hash = hashlib.sha256((self.data / "installation-mode.json").read_bytes()).hexdigest() \
            if (self.data / "installation-mode.json").exists() else None
        self.note(f"install exit 0; current 9.0.0; data schema 0 -> 1; backup {backups[0].name}")
        self.note("launcher: " + str(self.launcher("nature-worker")))

    def s_seed_work(self) -> None:
        self.start_worker("9.0.0")
        self.mode_hash = hashlib.sha256((self.data / "installation-mode.json").read_bytes()).hexdigest()
        put = self.api("PUT", "/api/settings", json={"provider": "openai_compatible", "model": "fake",
                                                     "endpoint": self.model.url, "api_key": SECRET})
        expect(put.status_code == 200 and put.json()["api_key_source"] == "stored", f"settings: {put.text}")
        conv = self.api("POST", "/api/conversations").json()["id"]
        self.model.queue = [{"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "delete_file", "arguments": '{"path": "keep.txt"}'}}]}]
        sent = self.api("POST", f"/api/conversations/{conv}/messages", json={"text": "delete keep.txt"})
        expect(sent.status_code == 200, f"send: {sent.text}")
        detail = self.api("GET", f"/api/conversations/{conv}").json()
        pending = [a for a in detail.get("approvals", []) if a["status"] == "pending"]
        expect(len(pending) == 1, f"expected one pending approval, got {detail.get('approvals')}")
        self.approval_conv = conv
        self.note(f"seeded: stored API key, conversation {conv[:8]} with a pending delete approval, workspace file")

    def _watch_stop_request(self) -> dict:
        """Records when the upgrader's stop request first appears in the data root."""
        seen: dict = {}

        def watch():
            while "at" not in seen and not seen.get("done"):
                if (self.data / "maintenance-request.json").exists():
                    seen["at"] = time.monotonic()
                time.sleep(0.02)
        threading.Thread(target=watch, daemon=True).start()
        return seen

    def _background_message(self, conv: str, text: str) -> dict:
        reply: dict = {}

        def send():
            reply["r"] = self.api("POST", f"/api/conversations/{conv}/messages", json={"text": text})
            reply["at"] = time.monotonic()
        reply["thread"] = threading.Thread(target=send)
        reply["thread"].start()
        return reply

    def s_upgrade_while_agent_runs(self) -> None:
        conv = self.api("POST", "/api/conversations").json()["id"]
        self.model.gate = self.data / "maintenance-request.json"
        self.model.delay, self.model.queue = 4, [{"role": "assistant", "content": "slow work finished"}]
        reply = self._background_message(conv, "long task")
        time.sleep(1.5)
        old_worker = self.worker
        stop_seen = self._watch_stop_request()
        result = self.upgrader("upgrade", "--bundle", self.bundles["B"], "--confirm-version", "9.1.0")
        reply["thread"].join(60)
        stop_seen["done"] = True
        self.model.delay, self.model.gate = 0, None
        expect(result.returncode == 0, f"upgrade exit {result.returncode}: {result.stdout[-1500:]}")
        expect(reply.get("r") is not None and reply["r"].status_code == 200,
               "the Agent run in progress was cut off by the upgrade")
        expect("at" in stop_seen and stop_seen["at"] < reply["at"],
               "the stop request did not arrive while the Agent run was still in progress")
        self.note(f"stop requested {reply['at'] - stop_seen['at']:.1f} s before the running Agent request finished")
        expect(old_worker.wait(timeout=10) == 0, "the old Worker did not exit cleanly")
        self.worker = None
        expect(self.current() == "9.1.0" and self.schema() == 2 and "e2e_2" in self.tables(),
               f"after upgrade: current {self.current()}, schema {self.schema()}")
        expect(not (self.data / "upgrade-in-progress.json").exists(), "upgrade marker left behind")
        expect(self.history()[-1]["outcome"] == "succeeded", "history does not record success")
        self.note("drain: the running Agent request completed (HTTP 200) before the old Worker exited (exit 0)")
        self.note("upgrade 9.0.0 -> 9.1.0 exit 0; data schema 1 -> 2; Worker not restarted by the upgrader")
        self.start_worker("9.1.0")
        expect(self.api("GET", "/api/settings").json()["api_key_source"] == "stored", "stored API key lost")
        detail = self.api("GET", f"/api/conversations/{conv}").json()
        expect(any(m["content"] == "slow work finished" for m in detail["messages"]), "work result lost")
        approvals = self.api("GET", f"/api/conversations/{self.approval_conv}").json()["approvals"]
        expect([a["status"] for a in approvals] == ["pending"], f"pending approval not preserved: {approvals}")
        expect((self.ws / "keep.txt").read_text() == "user file\n", "workspace file changed")
        expect(self.api("GET", "/api/health").headers.get("x-nature-worker-version") == "9.1.0",
               "version header for open pages missing")
        leaked = [str(p) for p in self.prefix.rglob("*") if p.is_file() and p.suffix in (".json", ".jsonl", ".log")
                  and SECRET in p.read_text(errors="ignore")]
        expect(not leaked, f"API key found in {leaked}")
        self.note("after restart on 9.1.0: API key still stored, result and pending approval preserved, "
                  "workspace unchanged, X-Nature-Worker-Version 9.1.0, key absent from journal/history/logs")

    def s_drain_timeout_aborts(self) -> None:
        conv = self.api("POST", "/api/conversations").json()["id"]
        self.model.gate = self.data / "maintenance-request.json"
        self.model.delay, self.model.queue = 20, [{"role": "assistant", "content": "long work finished"}]
        reply = self._background_message(conv, "very long task")
        time.sleep(1.5)
        result = self.upgrader("upgrade", "--bundle", self.bundles["E"], "--confirm-version", "9.4.0",
                               "--drain-timeout", "3")
        expect(result.returncode == 2 and self.history()[-1]["outcome"] == "aborted",
               f"expected aborted (exit 2), got {result.returncode}: {result.stdout[-600:]}")
        expect(self.current() == "9.1.0" and self.schema() == 2, "aborted upgrade changed the installation")
        expect(self.api("GET", "/api/health").json()["version"] == "9.1.0", "Worker not running after abort")
        for _ in range(10):  # the Worker notices the withdrawn stop request on its next poll (0.5 s)
            new = self.api("POST", "/api/conversations")
            if new.status_code == 200:
                break
            time.sleep(0.3)
        expect(new.status_code == 200, "Worker still refuses new work 3 s after the abort")
        reply["thread"].join(60)
        self.model.delay, self.model.gate = 0, None
        expect(reply["r"].status_code == 200, "running work failed after the abort")
        expect(not (self.data / "upgrade-in-progress.json").exists(), "marker left after abort")
        self.note("drain timeout 3 s while the Agent run needed 20 s more: exit 2 'aborted', 9.1.0 kept running, accepted "
                  "new work again, the running request completed")
        self.stop_worker()

    def s_refusals(self) -> None:
        cases = [("tampered-wheel", "does not match its SHA-256"), ("untrusted", "does not trust"),
                 ("altered-manifest", "signature is invalid"), ("A", "not newer"),
                 ("wrong-platform", "Python 3.99"), ("D", "needs 9.2.0 or later")]
        before = len(self.history())
        for name, needle in cases:
            version = "9.4.0" if name not in ("A", "D") else {"A": "9.0.0", "D": "9.3.0"}[name]
            result = self.upgrader("upgrade", "--bundle", self.bundles[name], "--confirm-version", version)
            expect(result.returncode == 2, f"{name}: exit {result.returncode}")
            expect(needle in result.stdout, f"{name}: reason {needle!r} not in output: {result.stdout[-500:]}")
            self.note(f"{name}: exit 2, '{needle}'")
        declined = self.upgrader("upgrade", "--bundle", self.bundles["E"])
        expect(declined.returncode == 2 and "Not authorized" in declined.stdout, "no-terminal upgrade was not refused")
        wrong = self.upgrader("upgrade", "--bundle", self.bundles["E"], "--confirm-version", "9.9.9")
        expect(wrong.returncode == 2 and "does not match" in wrong.stdout, "mismatched confirmation accepted")
        self.note("no authorization (no terminal, no --confirm-version): exit 2; wrong --confirm-version: exit 2")
        expect(self.current() == "9.1.0" and self.schema() == 2, "a refused bundle changed the installation")
        expect(not (self.prefix / "versions" / "9.3.0").exists(), "a refused release was installed")
        outcomes = [h["outcome"] for h in self.history()[before:]]
        expect(outcomes == ["refused", "refused", "refused", "ineligible", "ineligible", "ineligible",
                            "declined", "declined"], f"history: {outcomes}")
        self.note(f"history: {outcomes}; current 9.1.0 and data unchanged; 9.3.0 never installed")

    def s_failed_upgrade_rolls_back(self) -> None:
        self.start_worker("9.1.0")
        conversations = len(self.api("GET", "/api/conversations").json())
        result = self.upgrader("upgrade", "--bundle", self.bundles["C"], "--confirm-version", "9.2.0")
        self.worker = None
        expect(result.returncode == 1 and self.history()[-1]["outcome"] == "rolled_back",
               f"expected rolled_back, got {result.returncode}: {result.stdout[-800:]}")
        expect(self.current() == "9.1.0", "9.1.0 not active after rollback")
        expect(self.schema() == 2 and "e2e_3" not in self.tables(), "data not restored to schema 2")
        aside = list((self.data / "backups").glob("failed-upgrade-*"))
        expect(len(aside) == 1 and self.schema(aside[0] / "worker.db") == 3,
               "the migrated data was not kept aside")
        expect(not (self.data / "upgrade-in-progress.json").exists(), "marker left after rollback")
        self.note(f"9.1.0 -> 9.2.0 (broken startup): exit 1 'rolled_back', failed phase "
                  f"{self.history()[-1].get('failed_phase')}; data schema 3 kept aside in {aside[0].name}, "
                  "schema 2 restored")
        self.start_worker("9.1.0")
        expect(len(self.api("GET", "/api/conversations").json()) == conversations, "work history changed")
        self.note("9.1.0 starts again with the same work history")
        self.stop_worker()

    def _interrupt(self, phase: str, env: dict | None = None) -> None:
        cmd = [self.python(self.current()), "-I", "-m", "nature_worker.upgrade", "upgrade", "--prefix", self.prefix,
               "--bundle", self.bundles["E"], "--confirm-version", "9.4.0"]
        environment = {**os.environ, **(env or {})}
        log = open(self.w / "logs" / f"interrupted-{phase}.log", "wb")
        proc = subprocess.Popen([str(c) for c in cmd], env=environment, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                state = self.state()
                if state and state.get("phase") == phase:
                    time.sleep(0.5)
                    return
                if proc.poll() is not None:
                    raise Check(f"the upgrade ended before reaching {phase}")
                time.sleep(0.05)
            raise Check(f"phase {phase} not reached")
        finally:
            proc.kill()
            proc.wait()

    def s_killed_during_migration(self) -> None:
        self._interrupt("migrating", {"E2E_MIGRATION_SLEEP": "5"})
        expect(self.state()["phase"] == "migrating", "journal does not show the interrupted phase")
        self.note("upgrader process killed during 'migrating' (the migration itself still running)")
        refusal = self.worker_refuses_to_start()
        self.note(f"Worker start refused: {refusal[:160]}")
        status = self.upgrader("status")
        expect("UNFINISHED" in status.stdout, "status does not report the unfinished upgrade")
        declined = self.upgrader("recover")
        expect(declined.returncode == 2, "recovery ran without confirmation")
        time.sleep(6)  # let the orphaned migration finish: recovery must not depend on its timing
        recovered = self.upgrader("recover", "--confirm")
        expect(recovered.returncode == 0, f"recover failed: {recovered.stdout[-800:]}")
        expect(self.current() == "9.1.0" and self.schema() == 2 and "e2e_3" not in self.tables(),
               f"after recover: current {self.current()}, schema {self.schema()}")
        expect(self.state() is None and not (self.data / "upgrade-in-progress.json").exists(), "recovery incomplete")
        self.note("recover without --confirm: exit 2 (nothing changed); recover --confirm: 9.1.0 active, data "
                  "restored to schema 2 from this upgrade's backup")
        self.start_worker("9.1.0")
        self.stop_worker()

    def s_upgrade_after_recovery(self) -> None:
        result = self.upgrader("upgrade", "--bundle", self.bundles["E"], "--confirm-version", "9.4.0")
        expect(result.returncode == 0, f"upgrade failed: {result.stdout[-800:]}")
        expect(self.current() == "9.4.0" and self.schema() == 3, "9.4.0 not active with schema 3")
        from nature_worker.maintenance import worker_running
        expect(not worker_running(self.data), "the upgrader started a Worker that was not running before")
        self.start_worker("9.4.0")
        self.api("POST", "/api/conversations")  # work done only on 9.4.0
        self.conversations_on_e = len(self.api("GET", "/api/conversations").json())
        self.note(f"9.1.0 -> 9.4.0 exit 0, data schema 3; {self.conversations_on_e} conversations on 9.4.0")

    def s_user_rollback(self) -> None:
        result = self.upgrader("rollback", "--confirm")
        self.worker = None
        expect(result.returncode == 0, f"rollback failed: {result.stdout[-800:]}")
        expect(self.current() == "9.1.0" and self.schema() == 2, "rollback did not restore 9.1.0 and schema 2")
        aside = sorted((self.data / "backups").glob("rolled-back-*"))
        expect(aside and self.schema(aside[-1] / "worker.db") == 3, "work done on 9.4.0 was not kept aside")
        expect("is restored from the backup" in result.stdout, "the user was not told about the data restore")
        self.note(f"rollback --confirm: 9.4.0 -> 9.1.0; schema 3 data kept aside in {aside[-1].name}")
        self.start_worker("9.1.0")
        self.stop_worker()

    def s_killed_during_verification(self) -> None:
        self._interrupt("verifying")
        self.note("upgrader killed during 'verifying' (its health-check Worker left running)")
        recovered = self.upgrader("recover", "--confirm")
        expect(recovered.returncode == 0, f"recover failed: {recovered.stdout[-800:]}")
        expect(self.current() == "9.4.0" and self.schema() == 3, "recovery did not complete the verified upgrade")
        expect(self.history()[-1]["outcome"] == "succeeded", "history does not record the completed upgrade")
        self.note("recover --confirm re-verified 9.4.0, stopped the orphaned health check, completed the upgrade")
        self.start_worker("9.4.0")
        self.stop_worker()

    def s_separation(self) -> None:
        mode = self.data / "installation-mode.json"
        expect(hashlib.sha256(mode.read_bytes()).hexdigest() == self.mode_hash,
               "an upgrade changed the installation-mode marker")
        expect(json.loads(mode.read_text())["mode"] == "standalone", "mode is not standalone")
        sc = self.w / "sc-data"
        sc.mkdir(mode=0o700)
        (sc / "installation-mode.json").write_text('{"schema": 1, "mode": "platform_client"}\n')
        result = self.run([self.python(self.current()), "-I", "-m", "nature_worker.upgrade", "install", "--bundle",
                           self.bundles["E"], "--prefix", self.w / "prefix2", "--data-dir", sc,
                           "--trust-key", self.trust, "--confirm-version", "9.4.0"])
        expect(result.returncode == 3 and "Platform Client" in result.stdout, f"S-C data accepted: {result.stdout}")
        self.note("installation-mode marker unchanged by all upgrades (standalone); a Standalone install on a "
                  "Platform Client data root is refused (exit 3)")
        history = self.upgrader("history")
        self.note("history:\n```\n" + history.stdout.strip() + "\n```")

    def report(self) -> int:
        lines = ["# Upgrade end-to-end report", "",
                 f"- Host: {sys.platform}, Python {sys.version.split()[0]}",
                 f"- Disposable installation: `{self.prefix}` (data `{self.data}`)",
                 f"- Date: {time.strftime('%Y-%m-%d %H:%M:%S %z')}", ""]
        for name, ok, evidence in self.results:
            lines.append(f"## {'PASS' if ok else 'FAIL'}: {name}")
            lines += [f"- {e}" if not e.startswith("```") and "\n" not in e else e for e in evidence]
            lines.append("")
        (self.w / "upgrade-e2e-report.md").write_text("\n".join(lines), encoding="utf-8")
        passed = sum(ok for _, ok, _ in self.results)
        print(f"{passed}/{len(self.results)} scenarios passed; report: {self.w / 'upgrade-e2e-report.md'}")
        return 0 if passed == len(self.results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workdir", type=Path, required=True, help="new directory for everything this test creates")
    parser.add_argument("--src", type=Path, help="source tree to test (default: committed HEAD)")
    args = parser.parse_args()
    if args.workdir.exists():
        parser.error(f"{args.workdir} exists: use a new directory")
    e2e = E2E(args.workdir.resolve(), args.src.resolve() if args.src else None)
    e2e.scenario("Build signed test releases 9.0.0-9.4.0 and invalid bundles", e2e.setup)
    if not e2e.results[-1][1]:
        return e2e.report()
    for name, fn in [
        ("Install 9.0.0 over existing unversioned data", e2e.s_install_over_existing_data),
        ("Seed real work: stored API key, pending approval", e2e.s_seed_work),
        ("Upgrade while Agent work runs (drain, preserve, restart)", e2e.s_upgrade_while_agent_runs),
        ("Drain timeout aborts without changes", e2e.s_drain_timeout_aborts),
        ("Refuse invalid, untrusted, incompatible and unauthorized upgrades", e2e.s_refusals),
        ("Failed upgrade rolls back code and data", e2e.s_failed_upgrade_rolls_back),
        ("Upgrade killed during migration, recovered", e2e.s_killed_during_migration),
        ("Successful upgrade after recovery", e2e.s_upgrade_after_recovery),
        ("User rollback restores compatible data", e2e.s_user_rollback),
        ("Upgrade killed during verification, recovered", e2e.s_killed_during_verification),
        ("Installation mode and edition separation", e2e.s_separation),
    ]:
        e2e.scenario(name, fn)
    if e2e.worker is not None:
        e2e.stop_worker()
    e2e.model.httpd.shutdown()
    return e2e.report()


if __name__ == "__main__":
    sys.exit(main())
