"""Runs the real server process (acceptance H and I).

Starts ``python -m nature_worker`` as a subprocess, drives it over HTTP,
restarts it, and checks that history survived.
"""

import ast
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from conftest import openai_text, openai_tool_call

SRC = Path(__file__).resolve().parents[1] / "src"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(tmp_path, port):
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env.update(
        NATURE_WORKER_DATA_DIR=str(tmp_path / "data"),
        NATURE_WORKER_WORKSPACE=str(tmp_path / "workspace"),
        NATURE_WORKER_PORT=str(port),
        PYTHONUNBUFFERED="1",
    )
    proc = subprocess.Popen([sys.executable, "-m", "nature_worker"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                return proc, base
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    raise AssertionError("server did not start:\n" + proc.stdout.read())


def _stop(proc):
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=15)
    return out


def test_history_persists_across_restart(tmp_path, fake_model):
    port = _free_port()
    proc, base = _start(tmp_path, port)
    key = "sk-process-test-SECRET-42424242"
    try:
        r = httpx.put(base + "/api/settings", json={"provider": "openai_compatible", "model": "fake",
                                                    "endpoint": fake_model.url + "/v1", "api_key": key})
        assert r.json()["configured"]
        conv_id = httpx.post(base + "/api/conversations").json()["id"]
        fake_model.queue(
            openai_tool_call(("write_file", {"path": "report.md", "content": "# Report\n"})),
            openai_text("Report written."),
        )
        r = httpx.post(f"{base}/api/conversations/{conv_id}/messages", json={"text": "write a report"}, timeout=60)
        assert r.json()["status"] == "done"
    finally:
        log1 = _stop(proc)

    proc, base = _start(tmp_path, port)
    try:
        convs = httpx.get(base + "/api/conversations").json()
        assert [c["id"] for c in convs] == [conv_id]
        conv = httpx.get(f"{base}/api/conversations/{conv_id}").json()
        assert [m["role"] for m in conv["messages"]] == ["user", "assistant", "tool", "assistant"]
        assert conv["messages"][-1]["content"] == "Report written."
        settings = httpx.get(base + "/api/settings").json()
        assert settings["configured"] and settings["api_key_source"] == "stored"
        assert (tmp_path / "workspace" / "report.md").read_text() == "# Report\n"
    finally:
        log2 = _stop(proc)

    assert "Nature AI Worker is running" in log1
    assert key not in log1 + log2


def test_no_platform_dependencies():
    """Acceptance I: the package imports only the standard library, its own
    modules and its declared third-party dependencies."""
    allowed_third_party = {"fastapi", "uvicorn", "httpx", "httpcore", "httpx2", "openai", "anthropic",
                           "google", "cryptography", "pydantic"}
    found = set()
    for path in (SRC / "nature_worker").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    third_party = {m for m in found if m not in sys.stdlib_module_names and m != "nature_worker"}
    assert third_party <= allowed_third_party, third_party - allowed_third_party
    text = " ".join(p.read_text().lower() for p in SRC.rglob("*") if p.suffix in (".py", ".js", ".html", ".css"))
    for marker in ("nature_ai_platform", "nature-ai-platform", "nature_platform"):
        assert marker not in text
