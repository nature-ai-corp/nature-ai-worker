"""Provider adapters and failure reporting (acceptance G).

Each adapter is driven through the real application and the real SDK against
the local fake server speaking that provider's wire format.
"""

import base64
import socket

import pytest

from conftest import (anthropic_response, configure, conversation, gemini_response, new_conversation,
                      ollama_response, openai_error, openai_text, openai_tool_call, send)


def _last_notice(client, conv_id):
    msg = conversation(client, conv_id)["messages"][-1]
    assert msg["role"] == "notice"
    return msg


# -- G: graceful failure reporting ----------------------------------------------

@pytest.mark.parametrize("status,message,kind,phrase", [
    (401, "Incorrect API key provided", "auth", "rejected the API key"),
    (404, "The model `nope` does not exist", "not_found", "could not find this model"),
    (429, "Rate limit reached", "rate_limit", "rate-limiting"),
    (400, "This model's maximum context length is 8192 tokens", "context_length", "too long"),
    (500, "boom", "server", "internal error"),
])
def test_provider_http_errors_are_explained(client, fake_model, status, message, kind, phrase):
    configure(client, endpoint=fake_model.url + "/v1")
    # The SDK retries 429/5xx; queue enough copies.
    fake_model.queue(*[openai_error(status, message)] * 4)
    conv_id = new_conversation(client)
    assert send(client, conv_id, "hi") == "error"
    notice = _last_notice(client, conv_id)
    assert notice["meta"]["error"] == kind
    assert phrase in notice["content"]
    assert f"(Details: {message})" in notice["content"]  # the provider's own message, not a raw dict


def test_unreachable_provider(client):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed port: connection refused
    configure(client, provider="ollama", endpoint=f"http://127.0.0.1:{port}", api_key=None)
    conv_id = new_conversation(client)
    assert send(client, conv_id, "hi") == "error"
    notice = _last_notice(client, conv_id)
    assert notice["meta"]["error"] == "network"
    assert "Could not connect" in notice["content"]


def test_ollama_model_without_tool_support(client, fake_model):
    configure(client, provider="ollama", endpoint=fake_model.url, api_key=None)
    fake_model.queue((400, {"error": "registry.ollama.ai/library/gemma2:2b does not support tools"}))
    conv_id = new_conversation(client)
    assert send(client, conv_id, "hi") == "error"
    notice = _last_notice(client, conv_id)
    assert notice["meta"]["error"] == "unsupported_tools"
    assert "does not support tool calling" in notice["content"]


def test_connection_test_endpoint(client, fake_model):
    fake_model.queue(openai_tool_call(("calculate", {"expression": "6*7"})))
    r = client.post("/api/settings/test", json={"provider": "openai_compatible", "model": "m",
                                                "endpoint": fake_model.url + "/v1"}).json()
    assert r == {"ok": True, "tools": True, "message": "Connected. The model called a tool successfully."}

    fake_model.queue(openai_text("42"))
    r = client.post("/api/settings/test", json={"provider": "openai_compatible", "model": "m",
                                                "endpoint": fake_model.url + "/v1"}).json()
    assert r["ok"] and not r["tools"]

    fake_model.queue(*[openai_error(401, "bad key")])
    r = client.post("/api/settings/test", json={"provider": "openai_compatible", "model": "m",
                                                "endpoint": fake_model.url + "/v1"}).json()
    assert not r["ok"] and "API key" in r["message"]


def test_missing_key_is_a_setup_error(client):
    view = configure(client, provider="openai", model="gpt-x", api_key=None)
    assert view["configured"] is False
    r = client.post("/api/settings/test", json={"provider": "anthropic", "model": "m"}).json()
    assert not r["ok"] and "No API key" in r["message"]


# -- adapter wire formats ------------------------------------------------------------

def test_anthropic_adapter(client, config, fake_model, monkeypatch):
    from nature_worker import providers

    monkeypatch.setattr(providers, "ANTHROPIC_BASE_URL", fake_model.url)
    (config.workspace_dir / "a.txt").write_text("alpha")
    configure(client, provider="anthropic", model="claude-test", api_key="sk-ant-test-123456")
    fake_model.queue(
        anthropic_response(("read_file", {"path": "a.txt"}), ("list_files", {}), text="Let me look."),
        anthropic_response(text="It says alpha."),
    )
    conv_id = new_conversation(client)
    assert send(client, conv_id, "what is in a.txt?") == "done"
    assert conversation(client, conv_id)["messages"][-1]["content"] == "It says alpha."

    req = fake_model.requests[-1]
    assert req["path"] == "/v1/messages"
    assert req["headers"]["x-api-key"] == "sk-ant-test-123456"
    body = req["body"]
    assert body["system"].startswith("You are Nature AI Worker")
    assert body["tools"][0]["input_schema"]["type"] == "object"
    # user / assistant(text + 2 tool_use) / user(2 tool_results merged into one message)
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert [b["type"] for b in body["messages"][1]["content"]] == ["text", "tool_use", "tool_use"]
    results = body["messages"][2]["content"]
    assert [(b["type"], b["tool_use_id"]) for b in results] == [("tool_result", "toolu_0"), ("tool_result", "toolu_1")]
    assert results[0]["content"] == "alpha"


def test_gemini_adapter_returns_thought_signatures(client, config, fake_model, monkeypatch):
    from nature_worker import providers

    monkeypatch.setattr(providers, "GEMINI_BASE_URL", fake_model.url + "/")
    (config.workspace_dir / "a.txt").write_text("alpha")
    configure(client, provider="gemini", model="gemini-test", api_key="gem-test-123456")
    sig = base64.b64encode(b"opaque-signature").decode()
    fake_model.queue(
        gemini_response(("read_file", {"path": "a.txt"}), signature=sig),
        gemini_response(text="It says alpha."),
    )
    conv_id = new_conversation(client)
    assert send(client, conv_id, "what is in a.txt?") == "done"
    assert conversation(client, conv_id)["messages"][-1]["content"] == "It says alpha."

    req = fake_model.requests[-1]
    assert req["path"].endswith("/models/gemini-test:generateContent")
    assert req["headers"]["x-goog-api-key"] == "gem-test-123456"
    contents = req["body"]["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    call_part = contents[1]["parts"][0]
    assert call_part["functionCall"]["name"] == "read_file"
    assert call_part["thoughtSignature"] == sig
    assert "id" not in call_part["functionCall"]  # Gemini did not send an id, so none is echoed
    resp_part = contents[2]["parts"][0]["functionResponse"]
    assert resp_part["name"] == "read_file" and resp_part["response"] == {"result": "alpha"}
    assert req["body"]["tools"][0]["functionDeclarations"][0]["name"]


def test_ollama_adapter(client, config, fake_model):
    (config.workspace_dir / "a.txt").write_text("alpha")
    configure(client, provider="ollama", model="qwen3:8b", endpoint=fake_model.url, api_key=None)
    fake_model.queue(ollama_response(("read_file", {"path": "a.txt"})), ollama_response(text="alpha!"))
    conv_id = new_conversation(client)
    assert send(client, conv_id, "read a.txt") == "done"
    req = fake_model.requests[-1]
    assert req["path"] == "/api/chat"
    body = req["body"]
    assert body["stream"] is False and body["model"] == "qwen3:8b"
    assert body["messages"][2]["tool_calls"][0]["function"] == {"name": "read_file", "arguments": {"path": "a.txt"}}
    assert body["messages"][3] == {"role": "tool", "content": "alpha", "tool_name": "read_file"}
    assert "authorization" not in req["headers"]


def test_deepseek_reasoning_content_is_returned(client, fake_model, monkeypatch):
    from nature_worker import providers

    monkeypatch.setattr(providers, "DEEPSEEK_BASE_URL", fake_model.url)
    configure(client, provider="deepseek", model="deepseek-reasoner", api_key="ds-test-123456")
    first = openai_tool_call(("list_files", {}))
    first[1]["choices"][0]["message"]["reasoning_content"] = "I should list files."
    fake_model.queue(first, openai_text("Empty workspace."))
    conv_id = new_conversation(client)
    assert send(client, conv_id, "what files?") == "done"
    assistant = fake_model.requests[-1]["body"]["messages"][2]
    assert assistant["reasoning_content"] == "I should list files."
