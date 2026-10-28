# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Ollama context window: Ollama silently drops the start of a prompt that does not fit (system
instructions included). The adapter requests the configured size, refuses a prompt that will not
fit, rejects an answer to a prompt Ollama shrank, and keeps tool results within the window."""
import pytest

from conftest import configure, conversation, new_conversation, ollama_response, send


def counted(response, prompt_tokens):
    status, body = response
    body["prompt_eval_count"] = prompt_tokens
    return status, body


def notice(client, conv_id):
    last = conversation(client, conv_id)["messages"][-1]
    assert last["role"] == "notice", last
    return last


@pytest.fixture
def ollama(client, config, fake_model):
    def setup(**settings):
        configure(client, provider="ollama", model="qwen3:8b", endpoint=fake_model.url, api_key=None, **settings)
        return client, config.workspace_dir, fake_model
    return setup


def test_the_configured_context_size_is_requested_and_otherwise_left_to_the_server(ollama):
    client, _, model = ollama(context_window=16384)
    model.queue(ollama_response(text="ok"))
    send(client, new_conversation(client), "hi")
    assert model.requests[-1]["body"]["options"] == {"num_ctx": 16384}
    client, _, model = ollama()
    model.queue(ollama_response(text="ok"))
    send(client, new_conversation(client), "hi")
    assert "options" not in model.requests[-1]["body"]
    assert client.get("/api/settings").json()["context_window"] is None


def test_a_prompt_that_cannot_fit_is_never_sent(ollama):
    client, ws, model = ollama(context_window=4096)
    conv_id = new_conversation(client)
    assert send(client, conv_id, "x" * 9000) == "error"    # ~4,500 tokens at the safe default estimate
    assert model.requests == [] and "no longer fits" in notice(client, conv_id)["content"]
    assert "Start a new conversation" in notice(client, conv_id)["content"]


def test_an_answer_to_a_prompt_ollama_shrank_is_rejected(ollama):
    client, ws, model = ollama()
    (ws / "data.txt").write_text("row\n" * 3000)
    model.queue(counted(ollama_response(("read_file", {"path": "data.txt"})), 900),
                counted(ollama_response(text="an answer without the instructions"), 450))  # fewer tokens than before
    conv_id = new_conversation(client)
    assert send(client, conv_id, "summarize data.txt") == "error"
    assert "no longer fits" in notice(client, conv_id)["content"]
    assert not any(m["content"] == "an answer without the instructions" for m in conversation(client, conv_id)["messages"])


def test_the_servers_window_is_learned_and_tool_results_stay_within_it(ollama):
    client, ws, model = ollama()
    model.get_payload = {"models": [{"name": "qwen3:8b", "model": "qwen3:8b", "context_length": 8192}]}
    (ws / "big.txt").write_text("".join(f"line {i:05d} of the report\n" for i in range(6000)))   # 156,000 characters
    model.queue(counted(ollama_response(("read_file", {"path": "big.txt"})), 1000),
                counted(ollama_response(text="read it in parts"), 1700))
    conv_id = new_conversation(client)
    assert send(client, conv_id, "read big.txt") == "done"
    result = [m for m in conversation(client, conv_id)["messages"] if m["role"] == "tool"][0]["content"]
    first_prompt = model.requests[0]["body"]["messages"]
    ratio = 1000 / (sum(len(m["content"]) for m in first_prompt) + len(__import__("json").dumps(model.requests[0]["body"]["tools"])))
    assert len(result) < 8192 * 0.25 / ratio + 400       # a quarter of the window (plus the continuation note)
    assert "offset=" in result and "search_text" in result


def test_without_a_known_window_nothing_is_refused_or_cut(ollama):
    client, ws, model = ollama()
    (ws / "big.txt").write_text("y" * 150_000)
    model.queue(ollama_response(("read_file", {"path": "big.txt"})), ollama_response(text="ok"))
    conv_id = new_conversation(client)
    assert send(client, conv_id, "read big.txt") == "done"
    result = [m for m in conversation(client, conv_id)["messages"] if m["role"] == "tool"][0]["content"]
    assert result.startswith("y" * 100_000) and "offset=100000" in result   # the previous behaviour
