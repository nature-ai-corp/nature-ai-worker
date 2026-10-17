"""Adapter for the Anthropic Claude Messages API."""

from __future__ import annotations

import anthropic

from . import ChatResult, ToolCall, classify_status, provider_error, sdk_error_detail

MAX_TOKENS = 8192


def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Convert neutral history. Tool results become user-role tool_result blocks,
    and consecutive messages with the same role are merged as the API requires."""
    out: list[dict] = []

    def append(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        if m["role"] == "user":
            append("user", [{"type": "text", "text": m["content"] or "(empty)"}])
        elif m["role"] == "assistant":
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["arguments"]})
            append("assistant", blocks or [{"type": "text", "text": "(no content)"}])
        elif m["role"] == "tool":
            append("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"] or "(empty)"}])
    return out


class AnthropicChat:
    def __init__(self, model: str, api_key: str, base_url: str):
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key, base_url=base_url, timeout=180, max_retries=2)

    def chat(self, system: str, messages: list[dict], tools: list[dict]) -> ChatResult:
        kwargs: dict = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": _to_anthropic_messages(messages),
        }
        if tools:
            kwargs["tools"] = [
                {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools
            ]
        try:
            resp = self.client.messages.create(**kwargs)
        except anthropic.APITimeoutError as e:
            raise provider_error("timeout") from e
        except anthropic.APIConnectionError as e:
            raise provider_error("network", str(e)) from e
        except anthropic.APIStatusError as e:
            raise classify_status(e.status_code, sdk_error_detail(e)) from e
        except anthropic.AnthropicError as e:
            raise provider_error("unknown", str(e)) from e

        texts, calls = [], []
        for block in resp.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=block.id, name=block.name, arguments=args))
        return ChatResult(text="\n".join(texts), tool_calls=calls)
