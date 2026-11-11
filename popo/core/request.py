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
        messages = []
        system = data.get("system")
        if system:
            messages.append({"role": "system", "content": system})
        for m in data.get("messages", []):
            role = "user" if m.get("role") in ("user", "human") else "assistant"
            content = m.get("content", "")
            if isinstance(content, list):
                # Only keep text blocks for now; multipart is future work.
                text_parts = [b["text"] for b in content if b.get("type") == "text"]
                content = "\n".join(text_parts)
            messages.append({"role": role, "content": content})
        max_tokens = _int_or_none(data.get("max_tokens"))
        # Anthropic default max_tokens is 4096; preserve it exactly if given.
        return cls(
            model=data.get("model", ""),
            messages=messages,
            stream=bool(data.get("stream", False)),
            temperature=data.get("temperature"),
            max_tokens=max_tokens,
            top_p=data.get("top_p"),
            extra={k: v for k, v in data.items() if k not in {
                "model", "messages", "stream", "temperature", "max_tokens", "top_p", "system",
            }},
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


def strip_internal_message_names(messages: list[dict]) -> list[dict]:
    """Remove proxy-only armor markers before forwarding messages upstream."""
    result = []
    for message in messages:
        name = str(message.get("name") or "")
        if (name.startswith("_popo_armor:")
                or name.startswith("_popo_armor_policy:")
                or name == "_popo_armor_finalizer"):
            message = dict(message)
            message.pop("name", None)
        result.append(message)
    return result


def render_upstream_messages(messages: list[dict]) -> list[dict]:
    """Render the injected policy envelope in V1's single-system-message shape.

    Keep separate messages inside the pipeline so compression, handoff, and
    replay handling can identify the protected policy span. At the provider
    boundary, join the leading system messages into one system message with the
    opaque policy text first. Some OpenAI-compatible gateways only honor the
    first system message; sending the client note and precedence tail in
    separate messages made policy delivery depend on provider behavior.
    """
    if not messages:
        return []

    first = messages[0]
    if not isinstance(first, dict):
        return strip_internal_message_names(messages)
    marker = str(first.get("name") or "")
    if first.get("role") != "system" or not marker.startswith("_popo_armor_policy:"):
        return strip_internal_message_names(messages)

    leading_system = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "system":
            break
        content = message.get("content", "")
        if isinstance(content, str) and content:
            leading_system.append(content)
        elif content not in (None, ""):
            # Preserve any unusual system payload as text without changing the
            # policy prefix. Normalized client notes are already plain strings.
            leading_system.append(str(content))
        index += 1

    rendered = [{"role": "system", "content": "\n\n".join(leading_system)}]
    rendered.extend(messages[index:])
    return strip_internal_message_names(rendered)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None
