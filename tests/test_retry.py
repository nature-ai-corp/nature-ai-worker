# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Bounded retries of model requests: transient failures only, at most two, never for a timeout."""
import pytest

from conftest import configure, conversation, new_conversation, ollama_response, send
from nature_worker import providers
from nature_worker.providers import ChatResult, ProviderError, _RetryingProvider, make_provider, provider_error


class Flaky:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def chat(self, system, messages, tools):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, ProviderError):
            raise outcome
        return outcome


@pytest.mark.parametrize("kind", ["network", "server", "rate_limit"])
def test_transient_failures_are_retried_then_succeed(kind):
    slept = []
    inner = Flaky(provider_error(kind), ChatResult("ok", []))
    assert _RetryingProvider(inner, sleep=slept.append).chat("s", [], []).text == "ok"
    assert inner.calls == 2 and slept == [2.0]


def test_retries_are_bounded_and_the_error_is_reported():
    slept = []
    inner = Flaky(*[provider_error("server", "502 bad gateway")] * 5)
    with pytest.raises(ProviderError) as failure:
        _RetryingProvider(inner, sleep=slept.append).chat("s", [], [])
    assert inner.calls == 3 and slept == [2.0, 6.0] and failure.value.kind == "server"


@pytest.mark.parametrize("kind", ["timeout", "auth", "not_found", "context_length", "bad_request", "unsupported_tools"])
def test_permanent_failures_and_timeouts_are_not_retried(kind):
    inner = Flaky(provider_error(kind), ChatResult("never", []))
    with pytest.raises(ProviderError):
        _RetryingProvider(inner, sleep=lambda s: None).chat("s", [], [])
    assert inner.calls == 1


def test_only_adapters_without_their_own_retries_are_wrapped():
    assert isinstance(make_provider("ollama", "m", None, "http://127.0.0.1:1").inner, _RetryingProvider)
    assert not isinstance(make_provider("ollama", "m", None, "http://127.0.0.1:1", retry=False).inner,
                          _RetryingProvider)
    assert not isinstance(make_provider("openai", "m", "sk-test-123456", None).inner, _RetryingProvider)


def test_a_failed_ollama_request_is_retried_in_a_real_run(client, fake_model, monkeypatch):
    monkeypatch.setattr(providers, "RETRY_DELAYS", (0.0, 0.0))
    configure(client, provider="ollama", model="qwen3:8b", endpoint=fake_model.url, api_key=None)
    fake_model.queue((503, {"error": "model is loading"}), ollama_response(text="ready"))
    conv_id = new_conversation(client)
    assert send(client, conv_id, "hi") == "done"
    assert conversation(client, conv_id)["messages"][-1]["content"] == "ready" and len(fake_model.requests) == 2
