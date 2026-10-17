"""Adapter for Ollama's native /api/chat endpoint."""

from __future__ import annotations

import uuid

import httpx

from . import ChatResult, ToolCall, classify_status, provider_error


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


class OllamaChat:
    # Local models can be slow, especially on first load.
    TIMEOUT = httpx.Timeout(600.0, connect=10.0)

    def __init__(self, model: str, endpoint: str, api_key: str | None = None):
        self.model = model
        self.url = endpoint.rstrip("/") + "/api/chat"
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def chat(self, system: str, messages: list[dict], tools: list[dict]) -> ChatResult:
        body: dict = {"model": self.model, "messages": _to_ollama_messages(system, messages), "stream": False}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]},
                }
                for t in tools
            ]
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
            msg = resp.json().get("message") or {}
        except ValueError as e:
            raise provider_error("unknown", "Ollama returned an invalid response") from e
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
        return ChatResult(text=msg.get("content") or "", tool_calls=calls)
