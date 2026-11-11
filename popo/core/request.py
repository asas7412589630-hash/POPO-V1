"""
Normalize incoming requests into an internal shape.

POPO supports two public shapes:
- OpenAI /v1/chat/completions
- Anthropic /v1/messages (converted to OpenAI-style internally)

This module focuses on **structural normalization**, not armor injection.
"""

from __future__ import annotations
import json
from typing import Any

from popo.errors import ClientError, ErrorKind


def _load_json(body: bytes) -> dict:
    if not body:
        raise ClientError(ErrorKind.BAD_REQUEST, "empty request body")
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise ClientError(ErrorKind.BAD_REQUEST, f"invalid JSON: {exc}")


class ChatRequest:
    """Canonical chat-completion request."""

    def __init__(
        self,
        model: str,
        messages: list[dict],
        stream: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        extra: dict | None = None,
    ):
        self.model = model
        self.messages = messages
        self.stream = stream
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.top_p = top_p
        self.extra = extra or {}

    @classmethod
    def from_openai(cls, body: bytes) -> "ChatRequest":
        data = _load_json(body)
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ClientError(ErrorKind.BAD_REQUEST, "messages must be a non-empty list")
        return cls(
            model=data.get("model", ""),
            messages=messages,
            stream=bool(data.get("stream", False)),
            temperature=data.get("temperature"),
            max_tokens=_int_or_none(data.get("max_tokens")),
            top_p=data.get("top_p"),
            extra={k: v for k, v in data.items() if k not in {
                "model", "messages", "stream", "temperature", "max_tokens", "top_p",
            }},
        )

    @classmethod
    def from_anthropic(cls, body: bytes) -> "ChatRequest":
        """Convert Anthropic /v1/messages to internal OpenAI-like shape."""
        data = _load_json(body)
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ClientError(ErrorKind.BAD_REQUEST, "messages must be a non-empty list")
        messages = []
        system = data.get("system")
        if system:
            messages.append({"role": "system", "content": _anthropic_system_text(system)})
        for m in raw_messages:
            if not isinstance(m, dict):
                raise ClientError(ErrorKind.BAD_REQUEST, "each message must be an object")
            if m.get("role") not in ("user", "human", "assistant"):
                raise ClientError(ErrorKind.BAD_REQUEST, "message role must be user or assistant")
            role = "user" if m.get("role") in ("user", "human") else "assistant"
            content = m.get("content", "")
            if isinstance(content, list):
                parts = []
                text_parts = []
                tool_calls = []
                had_user_content = False

                def flush_user_parts():
                    nonlocal had_user_content
                    if text_parts or parts:
                        value = list(parts)
                        if len(value) == 1 and value[0].get("type") == "text":
                            content_value = value[0]["text"]
                        else:
                            content_value = value
                        messages.append({"role": role, "content": content_value})
                        had_user_content = True
                        parts.clear()
                        text_parts.clear()

                for block in content:
                    if not isinstance(block, dict):
                        raise ClientError(ErrorKind.BAD_REQUEST,
                                          "Anthropic content blocks must be objects")
                    kind = block.get("type")
                    if kind == "text":
                        text = str(block.get("text", ""))
                        if role == "user":
                            parts.append({"type": "text", "text": text})
                        else:
                            text_parts.append(text)
                    elif kind == "image" and role == "user":
                        parts.append(_anthropic_image_part(block))
                    elif kind == "tool_use" and role == "assistant":
                        tool_input = block.get("input", {})
                        tool_calls.append({
                            "id": str(block.get("id") or ""),
                            "type": "function",
                            "function": {"name": str(block.get("name") or ""),
                                         "arguments": json.dumps(
                                             tool_input, ensure_ascii=False,
                                             separators=(",", ":"))},
                        })
                    elif kind == "tool_result" and role == "user":
                        flush_user_parts()
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            mapped_result = []
                            for part in result_content:
                                if not isinstance(part, dict):
                                    raise ClientError(ErrorKind.BAD_REQUEST,
                                                      "tool_result content blocks must be objects")
                                if part.get("type") == "text":
                                    mapped_result.append({"type": "text",
                                                          "text": str(part.get("text", ""))})
                                elif part.get("type") == "image":
                                    mapped_result.append(_anthropic_image_part(part))
                                else:
                                    raise ClientError(ErrorKind.BAD_REQUEST,
                                                      f"unsupported tool_result block: {part.get('type')}")
                            result_content = (
                                mapped_result[0]["text"] if len(mapped_result) == 1
                                and mapped_result[0].get("type") == "text" else mapped_result)
                        messages.append({"role": "tool",
                                         "tool_call_id": str(block.get("tool_use_id") or ""),
                                         "content": result_content})
                    else:
                        raise ClientError(ErrorKind.BAD_REQUEST,
                                          f"unsupported Anthropic content block: {kind}")

                if role == "assistant":
                    text = "\n".join(text_parts)
                    messages.append({"role": role, "content": text or None,
                                     **({"tool_calls": tool_calls} if tool_calls else {})})
                else:
                    flush_user_parts()
                    if not had_user_content and not any(
                            isinstance(block, dict) and block.get("type") == "tool_result"
                            for block in content):
                        messages.append({"role": role, "content": ""})
            else:
                messages.append({"role": role, "content": content})
        max_tokens = _int_or_none(data.get("max_tokens"))
        extra = {k: v for k, v in data.items() if k not in {
            "model", "messages", "stream", "temperature", "max_tokens", "top_p", "system",
            "tools", "tool_choice", "stop_sequences", "thinking",
        }}
        if isinstance(data.get("tools"), list):
            extra["tools"] = [{
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    **({"description": tool["description"]}
                       if tool.get("description") is not None else {}),
                    "parameters": tool.get("input_schema") or {"type": "object"},
                },
            } for tool in data["tools"] if isinstance(tool, dict)]
        tool_choice = data.get("tool_choice")
        if isinstance(tool_choice, dict):
            if tool_choice.get("type") == "tool":
                extra["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tool_choice.get("name", "")},
                }
            elif tool_choice.get("type") in ("auto", "any", "none"):
                extra["tool_choice"] = "required" if tool_choice["type"] == "any" \
                    else tool_choice["type"]
            if tool_choice.get("disable_parallel_tool_use") is True:
                extra["parallel_tool_calls"] = False
        elif isinstance(tool_choice, str):
            extra["tool_choice"] = "required" if tool_choice == "any" else tool_choice
        if data.get("stop_sequences") is not None:
            extra["stop"] = data["stop_sequences"]
        # Anthropic default max_tokens is 4096; preserve it exactly if given.
        return cls(
            model=data.get("model", ""),
            messages=messages,
            stream=bool(data.get("stream", False)),
            temperature=data.get("temperature"),
            max_tokens=max_tokens,
            top_p=data.get("top_p"),
            extra=extra,
        )

    def to_openai(self) -> dict:
        payload = {
            "model": self.model,
            "messages": self.messages,
            "stream": self.stream,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.top_p is not None:
            payload["top_p"] = self.top_p
        payload.update(self.extra)
        return payload


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _anthropic_system_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(block.get("text", "")) for block in value
                         if isinstance(block, dict) and block.get("type") == "text")
    raise ClientError(ErrorKind.BAD_REQUEST,
                      "system must be a string or a list of text blocks")


def _anthropic_image_part(block: dict) -> dict:
    source = block.get("source") or {}
    if source.get("type") == "base64" and source.get("data"):
        media_type = source.get("media_type") or "image/jpeg"
        url = f"data:{media_type};base64,{source['data']}"
    elif source.get("type") == "url" and source.get("url"):
        url = source["url"]
    else:
        raise ClientError(ErrorKind.BAD_REQUEST, "unsupported Anthropic image source")
    return {"type": "image_url", "image_url": {"url": url}}
