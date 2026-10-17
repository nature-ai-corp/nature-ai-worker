"""Adapter for the Google Gemini API (google-genai SDK)."""

from __future__ import annotations

import base64
import uuid

import httpx
from google import genai
from google.genai import errors, types

from . import ChatResult, ToolCall, classify_status, provider_error

try:  # newer google-genai versions may use httpx2 internally
    import httpx2

    _TIMEOUT_ERRORS: tuple = (httpx.TimeoutException, httpx2.TimeoutException)
    _NETWORK_ERRORS: tuple = (httpx.TransportError, httpx2.TransportError)
except ImportError:  # pragma: no cover
    _TIMEOUT_ERRORS = (httpx.TimeoutException,)
    _NETWORK_ERRORS = (httpx.TransportError,)


def _to_gemini_contents(messages: list[dict]) -> list[types.Content]:
    out: list[types.Content] = []

    def append(role: str, parts: list[types.Part]) -> None:
        if not parts:
            return
        if out and out[-1].role == role:
            out[-1].parts.extend(parts)
        else:
            out.append(types.Content(role=role, parts=parts))

    # Only echo call ids that Gemini generated itself; ids we invented are not sent.
    native_ids = {
        tc["id"]
        for m in messages
        for tc in (m.get("tool_calls") or [])
        if (tc.get("extra") or {}).get("native_id")
    }

    for m in messages:
        if m["role"] == "user":
            append("user", [types.Part(text=m["content"] or "(empty)")])
        elif m["role"] == "assistant":
            parts = []
            if m.get("content"):
                parts.append(types.Part(text=m["content"]))
            for tc in m.get("tool_calls") or []:
                sig = (tc.get("extra") or {}).get("thought_signature")
                parts.append(
                    types.Part(
                        function_call=types.FunctionCall(
                            name=tc["name"], args=tc["arguments"], id=tc["id"] if tc["id"] in native_ids else None
                        ),
                        thought_signature=base64.b64decode(sig) if sig else None,
                    )
                )
            append("model", parts or [types.Part(text="(no content)")])
        elif m["role"] == "tool":
            append(
                "user",
                [
                    types.Part(
                        function_response=types.FunctionResponse(
                            name=m.get("name") or "tool",
                            id=m["tool_call_id"] if m["tool_call_id"] in native_ids else None,
                            response={"result": m["content"]},
                        )
                    )
                ],
            )
    return out


class GeminiChat:
    def __init__(self, model: str, api_key: str, base_url: str):
        self.model = model
        self.client = genai.Client(
            api_key=api_key, vertexai=False, http_options=types.HttpOptions(base_url=base_url, timeout=180_000)
        )

    def chat(self, system: str, messages: list[dict], tools: list[dict]) -> ChatResult:
        config = types.GenerateContentConfig(
            system_instruction=system,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        if tools:
            config.tools = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t["name"], description=t["description"], parameters_json_schema=t["parameters"]
                        )
                        for t in tools
                    ]
                )
            ]
        try:
            resp = self.client.models.generate_content(
                model=self.model, contents=_to_gemini_contents(messages), config=config
            )
        except errors.APIError as e:
            raise classify_status(e.code, e.message or str(e)) from e
        except _TIMEOUT_ERRORS as e:
            raise provider_error("timeout") from e
        except _NETWORK_ERRORS as e:
            raise provider_error("network", str(e)) from e

        if not resp.candidates or not resp.candidates[0].content:
            reason = ""
            if resp.prompt_feedback and resp.prompt_feedback.block_reason:
                reason = f"blocked: {resp.prompt_feedback.block_reason}"
            elif resp.candidates:
                reason = f"finish reason: {resp.candidates[0].finish_reason}"
            raise provider_error("bad_request", f"Gemini returned no content ({reason or 'empty response'})")

        texts, calls = [], []
        for part in resp.candidates[0].content.parts or []:
            if part.function_call:
                fc = part.function_call
                extra: dict = {"native_id": True} if fc.id else {}
                if part.thought_signature:
                    extra["thought_signature"] = base64.b64encode(part.thought_signature).decode()
                calls.append(
                    ToolCall(id=fc.id or uuid.uuid4().hex, name=fc.name, arguments=dict(fc.args or {}), extra=extra)
                )
            elif part.text and not part.thought:
                texts.append(part.text)
        return ChatResult(text="".join(texts), tool_calls=calls)
