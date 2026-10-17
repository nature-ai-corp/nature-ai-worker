"""Adapter for OpenAI, DeepSeek and other OpenAI-compatible Chat Completions APIs."""

from __future__ import annotations

import json
import uuid

import openai

from . import ChatResult, ToolCall, classify_status, provider_error, sdk_error_detail


def _to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            msg: dict = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                    }
                    for tc in m["tool_calls"]
                ]
            reasoning = (m.get("meta") or {}).get("reasoning_content")
            if reasoning:
                # DeepSeek thinking mode expects reasoning to be returned during a tool-call turn.
                msg["reasoning_content"] = reasoning
            out.append(msg)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
    return out


def _to_openai_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]},
        }
        for t in tools
    ]


def parse_arguments(raw: str | None) -> tuple[dict, dict]:
    """Parse a JSON argument string. Returns (arguments, extra)."""
    if not raw:
        return {}, {}
    try:
        args = json.loads(raw)
        if isinstance(args, dict):
            return args, {}
    except json.JSONDecodeError:
        pass
    return {}, {"argument_error": f"Arguments were not a valid JSON object: {raw[:200]}"}


class OpenAIChat:
    def __init__(self, model: str, api_key: str | None, base_url: str):
        self.model = model
        # Local OpenAI-compatible servers often need no key, but the SDK requires a value.
        self.client = openai.OpenAI(api_key=api_key or "not-needed", base_url=base_url, timeout=180, max_retries=2)

    def chat(self, system: str, messages: list[dict], tools: list[dict]) -> ChatResult:
        kwargs: dict = {"model": self.model, "messages": _to_openai_messages(system, messages)}
        if tools:
            kwargs["tools"] = _to_openai_tools(tools)
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except openai.APITimeoutError as e:
            raise provider_error("timeout") from e
        except openai.APIConnectionError as e:
            raise provider_error("network", str(e)) from e
        except openai.APIStatusError as e:
            raise classify_status(e.status_code, sdk_error_detail(e)) from e
        except openai.OpenAIError as e:
            raise provider_error("unknown", str(e)) from e

        if not resp.choices:
            raise provider_error("unknown", "The provider returned no answer")
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            args, extra = parse_arguments(fn.arguments)
            calls.append(ToolCall(id=tc.id or uuid.uuid4().hex, name=fn.name, arguments=args, extra=extra))
        meta = {}
        reasoning = (msg.model_extra or {}).get("reasoning_content")
        if reasoning and calls:
            meta["reasoning_content"] = reasoning
        return ChatResult(text=msg.content or "", tool_calls=calls, meta=meta)
