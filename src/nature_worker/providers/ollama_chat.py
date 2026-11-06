# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Adapter for Ollama's native /api/chat endpoint.

Ollama does not reject a prompt longer than the context window: it silently drops the start of
it, system instructions and tool descriptions included, and answers normally (measured with
qwen3.8:27b on Ollama 0.32: a 41,733-token prompt with num_ctx 32768 was cut to 16,386 tokens,
the system instruction was ignored and the answer was wrong). So the adapter

- requests ``num_ctx`` when the user set a context size (else the server's default applies);
- learns the context window in use (that setting, or ``context_length`` from /api/ps) and the
  tokens per character of this conversation (from Ollama's own ``prompt_eval_count``);
- refuses to send a prompt estimated not to fit, and rejects an answer whose prompt Ollama
  shrank although the conversation grew. Both raise ``context_length``, never a silent answer.
"""

from __future__ import annotations

import json
import uuid

import httpx

from . import ChatResult, ProviderError, ToolCall, classify_status, provider_error


def _to_ollama_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            msg: dict = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {"function": {"name": tc["name"], "arguments": tc["arguments"]}} for tc in m["tool_calls"]
                ]
            out.append(msg)
        elif m["role"] == "tool":
            out.append({"role": "tool", "content": m["content"], "tool_name": m.get("name") or ""})
    return out


# Until Ollama has measured this conversation, assume half a token per character: typical text
# is about 0.25, number-heavy tables up to 0.6. Estimates above this share of the window refuse.
DEFAULT_TOKENS_PER_CHAR = 0.5
FIT_SHARE = 0.9


def _too_long(estimate: int, window: int, measured: bool) -> str:
    return (f"This conversation (about {estimate:,} tokens{'' if measured else ', estimated'}) no longer fits the "
            f"model's context window of {window:,} tokens. Ollama would silently drop its beginning, including "
            "the agent's instructions. Start a new conversation, or set a larger context size in Settings "
            "(Advanced) if the model and memory allow it.")


class OllamaChat:
    # Local models can be slow, especially on first load.
    TIMEOUT = httpx.Timeout(600.0, connect=10.0)

    def __init__(self, model: str, endpoint: str, api_key: str | None = None, *, context_window: int | None = None):
        self.model = model
        self.base = endpoint.rstrip("/")
        self.url = self.base + "/api/chat"
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.context_window = context_window
        self.window: int | None = context_window      # the window in use, once known
        self.last: tuple[int, int] | None = None       # (prompt characters, prompt tokens) of the last answer

    def tokens_per_char(self) -> float:
        return self.last[1] / self.last[0] if self.last and self.last[0] else DEFAULT_TOKENS_PER_CHAR

    def read_budget(self) -> int | None:
        """Characters one tool result may use: a quarter of the context window, if it is known."""
        return int(self.window * 0.25 / self.tokens_per_char()) if self.window else None

    def _loaded_window(self) -> int | None:
        try:
            for m in httpx.get(self.base + "/api/ps", headers=self.headers, timeout=5).json().get("models") or []:
                if m.get("name") == self.model or m.get("model") == self.model:
                    return int(m["context_length"]) if m.get("context_length") else None
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            pass
        return None

    def chat(self, system: str, messages: list[dict], tools: list[dict]) -> ChatResult:
        body: dict = {"model": self.model, "messages": _to_ollama_messages(system, messages), "stream": False}
        if self.context_window:
            body["options"] = {"num_ctx": self.context_window}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]},
                }
                for t in tools
            ]
        chars = sum(len(m.get("content") or "") + len(json.dumps(m.get("tool_calls") or "")) for m in body["messages"]) \
            + len(json.dumps(body.get("tools") or ""))
        if self.window:
            estimate = int(chars * self.tokens_per_char())
            if estimate > self.window * FIT_SHARE:
                raise ProviderError("context_length", _too_long(estimate, self.window, self.last is not None))
        try:
            resp = httpx.post(self.url, json=body, headers=self.headers, timeout=self.TIMEOUT)
        except httpx.TimeoutException as e:
            raise provider_error("timeout") from e
        except httpx.HTTPError as e:
            raise provider_error("network", f"{type(e).__name__} connecting to {self.url}") from e

        if resp.status_code != 200:
            try:
                detail = resp.json().get("error", resp.text)
            except ValueError:
                detail = resp.text
            raise classify_status(resp.status_code, str(detail))

        try:
            answer = resp.json()
            msg = answer.get("message") or {}
        except (ValueError, AttributeError) as e:
            raise provider_error("unknown", "Ollama returned an invalid response") from e
        tokens = answer.get("prompt_eval_count")
        if isinstance(tokens, int) and tokens > 0:
            if self.last and chars > self.last[0] and tokens < self.last[1]:
                # The conversation grew but Ollama read fewer tokens: it cut the prompt.
                raise ProviderError("context_length", _too_long(int(chars * self.last[1] / self.last[0]),
                                                                self.window or tokens, True))
            self.last = (chars, tokens)
            if self.window is None:
                self.window = self._loaded_window()
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            calls.append(
                ToolCall(
                    id=tc.get("id") or uuid.uuid4().hex,
                    name=fn.get("name", ""),
                    arguments=args if isinstance(args, dict) else {},
                    extra={} if isinstance(args, dict) else {"argument_error": f"Invalid arguments: {args!r}"[:200]},
                )
            )
        return ChatResult(text=msg.get("content") or "", tool_calls=calls,
                          truncated=answer.get("done_reason") == "length")
