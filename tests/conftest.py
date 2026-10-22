"""Shared fixtures.

``FakeModelServer`` is a local HTTP server that speaks the wire format of
each provider (OpenAI Chat Completions, Anthropic Messages, Gemini
generateContent, Ollama /api/chat). Tests queue scripted model responses;
the real Worker application and the real provider SDKs talk to it over HTTP.
This exercises the actual application end to end without network access.
It does NOT verify real provider services (see scripts/acceptance.py).
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from nature_worker.app import create_app
from nature_worker.config import Config


class FakeModelServer:
    def __init__(self):
        self.responses: list[tuple[int, dict]] = []
        self.requests: list[dict] = []
        self.delay = 0.0  # seconds to wait before each reply, to observe a run in progress
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self._reply(200, {"data": [], "models": []})

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                server.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                if not server.responses:
                    self._reply(500, {"error": {"message": "fake server: no scripted response left"}})
                    return
                status, payload = server.responses.pop(0)
                time.sleep(server.delay)
                self._reply(status, payload)

            def _reply(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def queue(self, *responses: tuple[int, dict]) -> None:
        self.responses.extend(responses)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# -- response builders for each wire format ------------------------------------


def openai_tool_call(*calls: tuple[str, dict], text: str | None = None) -> tuple[int, dict]:
    return 200, {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "fake",
        "choices": [{
            "index": 0, "finish_reason": "tool_calls",
            "message": {
                "role": "assistant", "content": text,
                "tool_calls": [
                    {"id": f"call_{name}_{i}", "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args)}}
                    for i, (name, args) in enumerate(calls)
                ],
            },
        }],
    }


def openai_text(text: str) -> tuple[int, dict]:
    return 200, {
        "id": "chatcmpl-2", "object": "chat.completion", "created": 0, "model": "fake",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
    }


def openai_error(status: int, message: str) -> tuple[int, dict]:
    return status, {"error": {"message": message, "type": "error", "code": None}}


def anthropic_response(*calls: tuple[str, dict], text: str | None = None) -> tuple[int, dict]:
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for i, (name, args) in enumerate(calls):
        content.append({"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": args})
    return 200, {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "fake", "content": content,
        "stop_reason": "tool_use" if calls else "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def gemini_response(*calls: tuple[str, dict], text: str | None = None, signature: str | None = None) -> tuple[int, dict]:
    parts = []
    if text:
        parts.append({"text": text})
    for name, args in calls:
        part = {"functionCall": {"name": name, "args": args}}
        if signature:
            part["thoughtSignature"] = signature
        parts.append(part)
    return 200, {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP", "index": 0}]}


def ollama_response(*calls: tuple[str, dict], text: str = "") -> tuple[int, dict]:
    return 200, {
        "model": "fake", "done": True,
        "message": {
            "role": "assistant", "content": text,
            "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in calls] or None,
        },
    }


# -- fixtures --------------------------------------------------------------------


@pytest.fixture
def fake_model():
    server = FakeModelServer()
    yield server
    server.close()


@pytest.fixture
def config(tmp_path, monkeypatch):
    from nature_worker.providers import SDK_ENV_OVERRIDES

    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY",
                "OPENAI_COMPATIBLE_API_KEY", "NATURE_WORKER_SECRET_KEY", *SDK_ENV_OVERRIDES):
        monkeypatch.delenv(var, raising=False)
    cfg = Config(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace",
                 allowed_hosts=["testserver", "localhost", "127.0.0.1"])
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def client(config):
    with TestClient(create_app(config)) as c:
        yield c


def configure(client, provider="openai_compatible", model="fake-model", endpoint=None, api_key="sk-test-secret-123456", **extra):
    body = {"provider": provider, "model": model, "endpoint": endpoint, "api_key": api_key, **extra}
    r = client.put("/api/settings", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def new_conversation(client) -> str:
    return client.post("/api/conversations").json()["id"]


def send(client, conv_id, text):
    r = client.post(f"/api/conversations/{conv_id}/messages", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()["status"]


def conversation(client, conv_id):
    return client.get(f"/api/conversations/{conv_id}").json()
