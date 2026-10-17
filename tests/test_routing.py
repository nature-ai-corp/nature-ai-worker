"""Provider endpoint selection and credential routing.

API keys must only ever reach the endpoint the user configured (or the
provider's official endpoint). SDK environment overrides must not redirect
requests or inject credentials, and stored keys for configurable endpoints are
bound to the endpoint they were saved for.
"""

import pytest

from conftest import (FakeModelServer, anthropic_response, configure, conversation, gemini_response,
                      new_conversation, openai_error, openai_text, send)
from nature_worker import providers
from nature_worker.credentials import CredentialStore

KEY = "sk-routing-SECRET-key-5150"


@pytest.fixture
def trap():
    """A server that must never receive a request."""
    server = FakeModelServer()
    yield server
    server.close()


OFFICIAL = [
    ("openai", "OPENAI_BASE_URL", "OPENAI_BASE_URL", "/v1", openai_text("ok")),
    ("anthropic", "ANTHROPIC_BASE_URL", "ANTHROPIC_BASE_URL", "", anthropic_response(text="ok")),
    ("gemini", "GEMINI_BASE_URL", "GOOGLE_GEMINI_BASE_URL", "/", gemini_response(text="ok")),
]


@pytest.mark.parametrize("provider,constant,env_var,suffix,reply", OFFICIAL)
def test_env_base_url_cannot_redirect_official_provider(client, fake_model, trap, monkeypatch,
                                                        provider, constant, env_var, suffix, reply):
    monkeypatch.setattr(providers, constant, fake_model.url + suffix)  # stands in for the official endpoint
    monkeypatch.setenv(env_var, trap.url + suffix)
    configure(client, provider=provider, model="m", api_key=KEY)
    fake_model.queue(reply)
    assert send(client, new_conversation(client), "hi") == "done"
    assert trap.requests == []
    assert len(fake_model.requests) == 1
    import os
    assert os.environ[env_var] == trap.url + suffix  # the user's environment is left as it was


def test_official_endpoints_are_explicit_defaults(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://attacker.invalid/v1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://attacker.invalid")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "http://attacker.invalid/")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    assert str(providers.make_provider("openai", "m", KEY, None).inner.client.base_url) == "https://api.openai.com/v1/"
    assert str(providers.make_provider("anthropic", "m", KEY, None).inner.client.base_url) == "https://api.anthropic.com"
    assert str(providers.make_provider("deepseek", "m", KEY, None).inner.client.base_url) == "https://api.deepseek.com"
    gem = providers.make_provider("gemini", "m", KEY, None).inner.client
    assert gem._api_client.vertexai is False
    assert gem._api_client._http_options.base_url == "https://generativelanguage.googleapis.com/"


def test_user_configured_compatible_endpoint_is_preserved(client, fake_model, trap, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", trap.url + "/v1")
    configure(client, endpoint=fake_model.url + "/v1", api_key=KEY)
    fake_model.queue(openai_text("ok"))
    assert send(client, new_conversation(client), "hi") == "done"
    assert trap.requests == []
    assert fake_model.requests[0]["headers"]["authorization"] == f"Bearer {KEY}"


def test_custom_header_env_vars_are_not_injected(client, fake_model, monkeypatch):
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "Authorization: Bearer AMBIENT-openai-token\nX-Leak: openai")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-ambient")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-ambient")
    configure(client, endpoint=fake_model.url + "/v1", api_key=None)  # keyless compatible server
    fake_model.queue(openai_text("ok"))
    send(client, new_conversation(client), "hi")
    headers = fake_model.requests[0]["headers"]
    assert "AMBIENT" not in str(headers) and "x-leak" not in headers
    assert "openai-organization" not in headers and "openai-project" not in headers


def test_anthropic_ambient_credentials_are_not_injected(client, fake_model, monkeypatch):
    monkeypatch.setattr(providers, "ANTHROPIC_BASE_URL", fake_model.url)
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Leak: anthropic")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "AMBIENT-anthropic-token")
    configure(client, provider="anthropic", model="m", api_key=KEY)
    fake_model.queue(anthropic_response(text="ok"))
    send(client, new_conversation(client), "hi")
    headers = fake_model.requests[0]["headers"]
    assert headers["x-api-key"] == KEY
    assert "AMBIENT" not in str(headers) and "x-leak" not in headers


def test_gemini_vertex_switch_and_google_api_key_env_are_ignored(client, fake_model, monkeypatch):
    monkeypatch.setattr(providers, "GEMINI_BASE_URL", fake_model.url + "/")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_API_KEY", "AMBIENT-google-key")
    configure(client, provider="gemini", model="m", api_key=KEY)
    fake_model.queue(gemini_response(text="ok"))
    assert send(client, new_conversation(client), "hi") == "done"
    assert fake_model.requests[0]["headers"]["x-goog-api-key"] == KEY


# -- stored keys are bound to their endpoint ------------------------------------------

def test_stored_key_is_not_sent_to_a_changed_endpoint(client, fake_model, trap):
    configure(client, endpoint=fake_model.url + "/v1", api_key=KEY)
    # The user switches to another endpoint without entering a key.
    view = configure(client, endpoint=trap.url + "/v1", api_key=None)
    assert view["api_key_source"] == "other_endpoint"
    assert view["key_endpoints"]["openai_compatible"] == fake_model.url.lower() + "/v1"
    trap.queue(openai_text("ok"), openai_text("ok"))
    send(client, new_conversation(client), "hi")
    client.post("/api/settings/test", json={"provider": "openai_compatible", "model": "m", "endpoint": trap.url + "/v1"})
    assert len(trap.requests) == 2
    assert all(KEY not in str(r) for r in trap.requests)

    # Switching back (trailing slash / case differences are normalized) uses the key again.
    view = configure(client, endpoint=fake_model.url.upper().replace("HTTP://", "http://") + "/v1/", api_key=None)
    assert view["api_key_source"] == "stored"
    fake_model.queue(openai_text("ok"))
    send(client, new_conversation(client), "hi")
    assert fake_model.requests[-1]["headers"]["authorization"] == f"Bearer {KEY}"


def test_ollama_key_is_bound_to_its_endpoint(client, fake_model, trap):
    from conftest import ollama_response

    configure(client, provider="ollama", model="m", endpoint=fake_model.url, api_key=KEY)
    fake_model.queue(ollama_response(text="ok"))
    send(client, new_conversation(client), "hi")
    assert fake_model.requests[0]["headers"]["authorization"] == f"Bearer {KEY}"
    configure(client, provider="ollama", model="m", endpoint=trap.url, api_key=None)
    trap.queue(ollama_response(text="ok"))
    send(client, new_conversation(client), "hi")
    assert "authorization" not in trap.requests[0]["headers"]


def test_fixed_endpoint_providers_ignore_endpoint_binding(config):
    store = CredentialStore(config.data_dir)
    store.set("openai", KEY, None)
    assert store.get("openai", None) == (KEY, "stored")
    assert store.all_values() == [KEY]


# -- secret redaction of provider output ---------------------------------------------

def test_key_echoed_by_endpoint_is_redacted(client, fake_model):
    configure(client, endpoint=fake_model.url + "/v1", api_key=KEY)
    fake_model.queue(openai_error(401, f"invalid key {KEY}"))
    conv_id = new_conversation(client)
    assert send(client, conv_id, "hi") == "error"
    fake_model.queue(openai_text(f"your key is {KEY}"))
    assert send(client, conv_id, "again") == "done"
    fake_model.queue(openai_error(401, f"invalid key {KEY}"))
    test = client.post("/api/settings/test", json={"provider": "openai_compatible", "model": "m",
                                                   "endpoint": fake_model.url + "/v1"}).json()
    conv = conversation(client, conv_id)
    assert KEY not in str(conv) and KEY not in str(test)
    assert "invalid key [REDACTED]" in conv["messages"][1]["content"]
    assert conv["messages"][-1]["content"] == "your key is [REDACTED]"
    assert KEY.encode() not in (client.app.state.config.data_dir / "worker.db").read_bytes()
