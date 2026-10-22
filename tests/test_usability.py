"""Usability fixes from user testing (WKR-022): new chat drafts, file preview, listen port."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

from conftest import openai_text
from nature_worker.app import MAX_PREVIEW_BYTES
from test_process import _free_port, _start, _stop


# -- UX-002: read-only file preview ---------------------------------------------------

@pytest.mark.parametrize("name, content", [
    ("notes.md", "# Notes\n- one\n"), ("readme.txt", "plain text"), ("sales.csv", "region,revenue\nNorth,10\n"),
    ("data.json", '{"a": 1}'), ("UPPER.CSV", "x,y\n"),
])
def test_preview_supported_text_files(client, config, name, content):
    (config.workspace_dir / name).write_text(content)
    r = client.get("/api/files/content", params={"path": name})
    assert r.status_code == 200
    assert r.json() == {"path": name, "content": content}


@pytest.mark.parametrize("name", ["report.docx", "image.png", "script.py", "archive.zip", "noext"])
def test_preview_refuses_other_types_but_download_works(client, config, name):
    (config.workspace_dir / name).write_bytes(b"PK\x03\x04 binary-ish")
    r = client.get("/api/files/content", params={"path": name})
    assert r.status_code == 415
    assert "Preview is available for .md, .txt, .csv and .json" in r.json()["detail"]
    assert "Download" in r.json()["detail"]
    d = client.get("/api/files/download", params={"path": name})
    assert d.status_code == 200 and d.content == b"PK\x03\x04 binary-ish"


def test_preview_refuses_oversized_files(client, config):
    (config.workspace_dir / "big.csv").write_bytes(b"a,b\n" * (MAX_PREVIEW_BYTES // 4 + 1))
    r = client.get("/api/files/content", params={"path": "big.csv"})
    assert r.status_code == 413
    assert "too large to preview" in r.json()["detail"] and "limit is 1 MB" in r.json()["detail"]
    assert client.get("/api/files/download", params={"path": "big.csv"}).status_code == 200


def test_preview_of_missing_file_or_folder(client, config):
    (config.workspace_dir / "sub.md").mkdir()
    assert client.get("/api/files/content", params={"path": "missing.md"}).status_code == 404
    assert client.get("/api/files/content", params={"path": "sub.md"}).status_code == 404


def test_preview_keeps_workspace_restrictions(client, config):
    outside = config.workspace_dir.parent / "secret.md"
    outside.write_text("TOP-SECRET")
    os.symlink(outside, config.workspace_dir / "link.md")
    for path in ["../secret.md", str(outside), "link.md", "sub/../../secret.md"]:
        r = client.get("/api/files/content", params={"path": path})
        assert r.status_code == 400, path
        assert "TOP-SECRET" not in r.text


def test_preview_refuses_binary_content_with_text_extension(client, config):
    (config.workspace_dir / "fake.txt").write_bytes(b"\x00\x01\x02binary")
    r = client.get("/api/files/content", params={"path": "fake.txt"})
    assert r.status_code == 400 and "binary" in r.json()["detail"]


def test_preview_does_not_change_the_file(client, config):
    path = config.workspace_dir / "notes.md"
    path.write_text("unchanged")
    before = path.stat().st_mtime_ns
    client.get("/api/files/content", params={"path": "notes.md"})
    assert path.read_text() == "unchanged" and path.stat().st_mtime_ns == before


# -- UX-004: listen port ------------------------------------------------------------------

def _run_cli(tmp_path, *args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY") and k != "NATURE_WORKER_PORT"}
    env.update(NATURE_WORKER_DATA_DIR=str(tmp_path / "data"), NATURE_WORKER_WORKSPACE=str(tmp_path / "workspace"),
               **(env_extra or {}))
    return subprocess.run([sys.executable, "-m", "nature_worker", *args], env=env,
                          capture_output=True, text=True, timeout=30)


def test_port_option_selects_the_listen_port(tmp_path):
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env.update(NATURE_WORKER_DATA_DIR=str(tmp_path / "data"), NATURE_WORKER_WORKSPACE=str(tmp_path / "workspace"),
               NATURE_WORKER_PORT="1", PYTHONUNBUFFERED="1")  # --port wins over the environment
    proc = subprocess.Popen([sys.executable, "-m", "nature_worker", "--port", str(port)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise AssertionError("server did not start on the chosen port")
    finally:
        out = _stop(proc)
    assert f"running at http://127.0.0.1:{port}" in out


def test_default_port_and_bind_address_are_unchanged():
    env = {k: v for k, v in os.environ.items() if not k.startswith("NATURE_WORKER_")}
    code = "from nature_worker.config import Config; c = Config.from_env(); print(c.host, c.port)"
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
    assert r.stdout.split() == ["127.0.0.1", "8080"]


def test_busy_port_gives_an_actionable_error_and_leaves_the_other_program_alone(tmp_path):
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        port = other.getsockname()[1]
        r = _run_cli(tmp_path, "--port", str(port))
        assert r.returncode == 1
        assert f"Port {port} is already in use" in r.stderr
        assert "--port" in r.stderr and "Traceback" not in r.stderr
        # The other program still owns the port and accepts connections.
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
    assert not (tmp_path / "data").exists()  # nothing was created


@pytest.mark.parametrize("args, env", [(("--port", "abc"), None), (("--port", "70000"), None),
                                       ((), {"NATURE_WORKER_PORT": "0"}), ((), {"NATURE_WORKER_PORT": "80a"})])
def test_invalid_port_is_explained(tmp_path, args, env):
    r = _run_cli(tmp_path, *args, env_extra=env)
    assert r.returncode == 2
    assert "invalid port" in r.stderr and "1 to 65535" in r.stderr and "Traceback" not in r.stderr


# -- UX-001 and UX-002 in a real browser ----------------------------------------------------

def _find_chrome():
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if path := shutil.which(name):
            return path
    return None


class Browser:
    """Minimal Chrome DevTools Protocol client: load a page and evaluate JavaScript in it."""

    def __init__(self, chrome: str, url: str):
        from websockets.sync.client import connect

        self.profile = tempfile.mkdtemp()
        port = _free_port()
        self.proc = subprocess.Popen(
            [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
             f"--remote-debugging-port={port}", f"--user-data-dir={self.profile}", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                pages = [t for t in httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=1).json()
                         if t["type"] == "page"]
                if pages:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            self.close()
            raise AssertionError("Chrome did not start")
        self.ws = connect(pages[0]["webSocketDebuggerUrl"], max_size=None)
        self.ids = 0

    def eval(self, expression: str):
        self.ids += 1
        self.ws.send(json.dumps({"id": self.ids, "method": "Runtime.evaluate", "params": {
            "expression": expression, "awaitPromise": True, "returnByValue": True}}))
        while True:
            msg = json.loads(self.ws.recv(timeout=30))
            if msg.get("id") == self.ids:
                result = msg["result"]
                if "exceptionDetails" in result:
                    raise AssertionError(result["exceptionDetails"])
                return result["result"].get("value")

    def wait(self, condition: str, timeout: float = 10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval(f"Boolean({condition})"):
                    return
            except AssertionError:
                pass
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for: {condition}")

    def send(self, method: str, params: dict | None = None):
        self.ids += 1
        self.ws.send(json.dumps({"id": self.ids, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv(timeout=30))
            if msg.get("id") == self.ids:
                return msg.get("result")

    def viewport(self, width: int, height: int, mobile: bool = False):
        self.send("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": mobile})

    def click(self, selector: str, text: str | None = None):
        """Click the first element matching the selector (and starting with the text, if given)."""
        match = f"x.textContent.trim().startsWith({json.dumps(text)})" if text else "true"
        self.wait(f"[...document.querySelectorAll({json.dumps(selector)})].some(x => {match})")
        self.eval(f"[...document.querySelectorAll({json.dumps(selector)})].find(x => {match}).click()")

    def text(self, selector: str) -> str:
        return self.eval(f"(document.querySelector({json.dumps(selector)}) || {{}}).textContent || ''")

    def close(self):
        if getattr(self, "ws", None):
            self.ws.close()
        self.proc.kill()
        self.proc.wait()
        shutil.rmtree(self.profile, ignore_errors=True)


@pytest.fixture
def ui(tmp_path, fake_model):
    chrome = _find_chrome()
    if not chrome:
        pytest.skip("Chrome/Chromium not installed")
    port = _free_port()
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "sales.csv").write_text("region,revenue\nNorth,170\n")
    (tmp_path / "workspace" / "report.docx").write_bytes(b"PK\x03\x04")
    proc, base = _start(tmp_path, port)
    configure_body = {"provider": "openai_compatible", "model": "fake", "endpoint": fake_model.url + "/v1",
                      "api_key": "sk-ui-test-DUMMY-0000"}
    assert httpx.put(base + "/api/settings", json=configure_body).json()["configured"]
    browser = Browser(chrome, base + "/")
    try:
        browser.wait("document.querySelector('#model-label').textContent.includes('fake')")
        yield browser, base, tmp_path
    finally:
        browser.close()
        _stop(proc)


def _open_work(b, title):
    b.click("#work-nav a", title)
    b.wait(f"!document.querySelector('#view-work').hidden && document.querySelector('#work-title').textContent === {json.dumps(title)}")


def test_new_work_clears_the_draft_and_keeps_drafts_per_work(ui, fake_model):
    b, base, tmp_path = ui
    for text in ("first work", "second work"):
        conv = httpx.post(base + "/api/conversations").json()["id"]
        fake_model.queue(openai_text(f"reply to {text}"))
        httpx.post(f"{base}/api/conversations/{conv}/messages", json={"text": text}, timeout=30)
    b.eval("loadWork()")
    b.wait("document.querySelectorAll('#work-nav a').length === 2")

    _open_work(b, "first work")
    b.eval("document.querySelector('#input').value = 'unsent draft for the first work'")
    b.click("#new-work")
    b.wait("!document.querySelector('#view-new').hidden")
    assert b.eval("document.querySelector('#new-input').value") == ""
    assert len(httpx.get(base + "/api/conversations").json()) == 2  # history kept

    b.eval("document.querySelector('#new-input').value = 'half-written new work'")
    _open_work(b, "second work")
    assert b.eval("document.querySelector('#input').value") == ""
    assert b.eval("[...document.querySelectorAll('#messages .msg')].map(m => m.textContent)") == \
        ["Yousecond work", "Agentreply to second work"]

    _open_work(b, "first work")
    assert b.eval("document.querySelector('#input').value") == "unsent draft for the first work"
    assert b.eval("[...document.querySelectorAll('#messages .msg')].map(m => m.textContent)") == \
        ["Youfirst work", "Agentreply to first work"]

    b.click("#new-work")
    b.wait("!document.querySelector('#view-new').hidden")
    assert b.eval("document.querySelector('#new-input').value") == ""  # cleared, not the earlier draft
    assert (tmp_path / "workspace" / "sales.csv").exists()


def test_clicking_a_file_shows_a_read_only_preview(ui):
    b, _, tmp_path = ui
    b.click("#open-files")
    b.wait("document.querySelectorAll('#file-list button.fname').length === 2")
    b.click("#file-list button.fname", "sales.csv")
    b.wait("!document.querySelector('#preview-content').hidden")
    assert b.eval("document.querySelector('#preview-dialog').open") is True
    assert b.text("#preview-content") == "region,revenue\nNorth,170\n"
    assert b.eval("document.querySelector('#preview-content').isContentEditable") is False
    assert b.eval("document.querySelector('#preview-label').hidden") is False
    assert b.eval("document.querySelector('#preview-dialog textarea, #preview-dialog input')") is None
    assert b.eval("document.querySelector('#preview-download').getAttribute('href')") == \
        "/api/files/download?path=sales.csv"

    b.click("#file-list button.fname", "report.docx")
    b.wait("document.querySelector('#preview-message').textContent.includes('Preview is available')")
    assert b.eval("document.querySelector('#preview-content').hidden") is True
    assert b.eval("document.querySelector('#preview-label').hidden") is True
    assert b.eval("document.querySelector('#preview-download').getAttribute('href')") == \
        "/api/files/download?path=report.docx"
    assert b.eval("document.querySelectorAll('#file-list a[title^=Download]').length") == 2  # Download kept
    assert (tmp_path / "workspace" / "sales.csv").read_text() == "region,revenue\nNorth,170\n"
