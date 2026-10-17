"""Common model interface and provider factory.

Conversation history is passed to providers in one neutral format::

    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [ToolCall-as-dict, ...], "meta": {...}}
    {"role": "tool", "tool_call_id": "...", "name": "...", "content": "..."}

Each adapter converts this to its own API format and returns a ``ChatResult``.
"""

from __future__ import annotations

import contextlib
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol
from urllib.parse import urlsplit, urlunsplit


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # Provider-specific data that must be sent back unchanged (e.g. Gemini thought signatures).
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {"id": self.id, "name": self.name, "arguments": self.arguments}
        if self.extra:
            d["extra"] = self.extra
        return d


@dataclass
class ChatResult:
    text: str
    tool_calls: list[ToolCall]
    # Provider-specific data stored with the assistant message (e.g. reasoning content).
    meta: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    """A provider failure with a message suitable for end users.

    ``kind`` is one of: auth, not_found, rate_limit, network, timeout,
    unsupported_tools, context_length, bad_request, server, config, unknown.
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


USER_MESSAGES = {
    "auth": "The provider rejected the API key. Check the key in Settings.",
    "not_found": "The provider could not find this model. Check the model name in Settings.",
    "rate_limit": "The provider is rate-limiting requests or your quota is exhausted. Wait a moment or check your account billing.",
    "network": "Could not connect to the provider. Check your internet connection and the endpoint URL.",
    "timeout": "The provider did not respond in time. Try again.",
    "unsupported_tools": "This model does not support tool calling, which the agent needs. Choose a tool-capable model.",
    "context_length": "This conversation is too long for the model. Start a new conversation.",
    "bad_request": "The provider rejected the request.",
    "server": "The provider reported an internal error. Try again later.",
    "config": "The model provider is not configured correctly.",
    "unknown": "The model call failed.",
}


def provider_error(kind: str, detail: str = "") -> ProviderError:
    message = USER_MESSAGES[kind]
    if detail:
        message = f"{message} (Details: {detail[:300]})"
    return ProviderError(kind, message)


def sdk_error_detail(exc: Exception) -> str:
    """Extract the provider's own error message from an SDK exception body."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]
        if isinstance(err, str):
            return err
    return getattr(exc, "message", None) or str(exc)


def classify_status(status: int | None, detail: str) -> ProviderError:
    """Map an HTTP status code and error text to a ProviderError."""
    low = detail.lower()
    if any(s in low for s in ("does not support tools", "tool use is not supported", "tools are not supported", "function calling is not enabled")):
        return provider_error("unsupported_tools", detail)
    if any(s in low for s in ("context length", "context_length", "too many tokens", "prompt is too long", "maximum context")):
        return provider_error("context_length", detail)
    if status in (401, 403):
        return provider_error("auth", detail)
    if status == 404:
        return provider_error("not_found", detail)
    if status == 429:
        return provider_error("rate_limit", detail)
    if status is not None and status >= 500:
        return provider_error("server", detail)
    if status is not None and 400 <= status < 500:
        return provider_error("bad_request", detail)
    return provider_error("unknown", detail)


class Provider(Protocol):
    def chat(self, system: str, messages: list[dict], tools: list[dict]) -> ChatResult: ...


PROVIDERS = {
    "openai": {"label": "OpenAI", "needs_key": True, "needs_endpoint": False},
    "anthropic": {"label": "Anthropic Claude", "needs_key": True, "needs_endpoint": False},
    "gemini": {"label": "Google Gemini", "needs_key": True, "needs_endpoint": False},
    "deepseek": {"label": "DeepSeek", "needs_key": True, "needs_endpoint": False},
    "ollama": {"label": "Ollama (local)", "needs_key": False, "needs_endpoint": True,
               "default_endpoint": "http://localhost:11434"},
    "openai_compatible": {"label": "OpenAI-compatible API", "needs_key": False, "needs_endpoint": True},
}

# Fixed endpoints for providers whose URL the user does not configure. They are always
# passed to the SDKs explicitly so SDK environment overrides cannot redirect API keys.
OPENAI_BASE_URL = "https://api.openai.com/v1"
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# SDK environment variables that would redirect requests or add headers/credentials
# behind the user's back. They are hidden while SDK clients are constructed (the
# SDKs read them only in their constructors). Endpoints come from Settings instead.
SDK_ENV_OVERRIDES = (
    "OPENAI_BASE_URL", "OPENAI_CUSTOM_HEADERS", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_ADMIN_KEY",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_AUTH_TOKEN",
    "GOOGLE_GEMINI_BASE_URL", "GOOGLE_VERTEX_BASE_URL", "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GENAI_USE_ENTERPRISE", "GOOGLE_API_KEY",
)
_env_lock = threading.Lock()


@contextlib.contextmanager
def isolated_sdk_env() -> Iterator[None]:
    with _env_lock:
        saved = {name: os.environ.pop(name) for name in SDK_ENV_OVERRIDES if name in os.environ}
        try:
            yield
        finally:
            os.environ.update(saved)


def normalize_endpoint(url: str | None) -> str | None:
    """Canonical form used to bind stored keys to an endpoint."""
    if not url:
        return None
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


def effective_endpoint(provider: str, endpoint: str | None) -> str | None:
    """The endpoint a provider's requests (and therefore its API key) will go to."""
    spec = PROVIDERS.get(provider, {})
    if not spec.get("needs_endpoint"):
        return None  # fixed official endpoint
    return normalize_endpoint(endpoint or spec.get("default_endpoint"))


class _RedactingProvider:
    """Keeps the API key out of error messages and replies even if an endpoint echoes it."""

    def __init__(self, inner: Provider, secret: str | None):
        self.inner, self.secret = inner, secret

    def _clean(self, text: str) -> str:
        return text.replace(self.secret, "[REDACTED]") if self.secret and len(self.secret) >= 6 else text

    def chat(self, system: str, messages: list[dict], tools: list[dict]) -> ChatResult:
        try:
            result = self.inner.chat(system, messages, tools)
        except ProviderError as e:
            raise ProviderError(e.kind, self._clean(e.message)) from None
        result.text = self._clean(result.text)
        return result


def make_provider(provider: str, model: str, api_key: str | None, endpoint: str | None) -> Provider:
    if provider not in PROVIDERS:
        raise provider_error("config", f"Unknown provider '{provider}'")
    if not model:
        raise provider_error("config", "No model selected")
    spec = PROVIDERS[provider]
    if spec["needs_key"] and not api_key:
        raise provider_error("config", "No API key set")
    if spec["needs_endpoint"] and not endpoint and not spec.get("default_endpoint"):
        raise provider_error("config", "No endpoint URL set")
    endpoint = endpoint or spec.get("default_endpoint")

    with isolated_sdk_env():
        if provider in ("openai", "deepseek", "openai_compatible"):
            from .openai_chat import OpenAIChat

            base_url = {"openai": OPENAI_BASE_URL, "deepseek": DEEPSEEK_BASE_URL}.get(provider, endpoint)
            adapter: Provider = OpenAIChat(model, api_key, base_url)
        elif provider == "anthropic":
            from .anthropic_chat import AnthropicChat

            adapter = AnthropicChat(model, api_key, ANTHROPIC_BASE_URL)
        elif provider == "gemini":
            from .gemini_chat import GeminiChat

            adapter = GeminiChat(model, api_key, GEMINI_BASE_URL)
        else:
            from .ollama_chat import OllamaChat

            adapter = OllamaChat(model, endpoint, api_key)
    return _RedactingProvider(adapter, api_key)
