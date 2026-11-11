"""Translate OpenAI chat-completion SSE into Anthropic Messages SSE.

The upstream transport yields arbitrary byte fragments, not complete SSE
events. This adapter therefore buffers event boundaries and maintains message,
content-block, tool-call, finish-reason, and usage state for the full stream.
"""

from __future__ import annotations

import json
from typing import Iterable, Iterator


_STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "stop_sequence",
}


def _event(name: str, payload: dict) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {data}\n\n".encode("utf-8")


def _split_frame(buffer: bytes) -> tuple[bytes, int] | None:
    candidates = [(buffer.find(b"\n\n"), 2), (buffer.find(b"\r\n\r\n"), 4)]
    candidates = [(index, size) for index, size in candidates if index >= 0]
    return min(candidates, default=None, key=lambda item: item[0])


def iter_anthropic_events(chunks: Iterable[bytes], *, fallback_model: str = "unknown") -> Iterator[bytes]:
    """Yield valid Anthropic ``/v1/messages`` events for an OpenAI SSE stream."""
    message_started = False
    message_finished = False
    text_block: int | None = None
    tools: dict[int, dict] = {}
    next_block = 0
    message_id = "msg_popo"
    model = fallback_model or "unknown"
    input_tokens = 0
    output_tokens = 0
    stop_reason = "end_turn"

    def start_message(obj: dict) -> list[bytes]:
        nonlocal message_started, message_id, model, input_tokens
        if message_started:
            return []
        message_started = True
        message_id = str(obj.get("id") or message_id)
        model = str(obj.get("model") or model)
        usage = obj.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        input_tokens = _as_nonnegative_int(usage.get("prompt_tokens"))
        return [_event("message_start", {
            "type": "message_start",
            "message": {
                "id": message_id, "type": "message", "role": "assistant",
                "model": model, "content": [], "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 0},
            },
        })]

    def start_text() -> tuple[int, list[bytes]]:
        nonlocal text_block, next_block
        if text_block is not None:
            return text_block, []
        text_block = next_block
        next_block += 1
        return text_block, [_event("content_block_start", {
            "type": "content_block_start", "index": text_block,
            "content_block": {"type": "text", "text": ""},
        })]

    def start_tool(state: dict) -> list[bytes]:
        nonlocal next_block
        if state["block"] is not None or not state["name"]:
            return []
        state["block"] = next_block
        next_block += 1
        state["started"] = True
        out = [_event("content_block_start", {
            "type": "content_block_start", "index": state["block"],
            "content_block": {
                "type": "tool_use",
                "id": state["id"] or f"toolu_popo_{state['index']}",
                "name": state["name"], "input": {},
            },
        })]
        if state["pending_args"]:
            out.append(_event("content_block_delta", {
                "type": "content_block_delta", "index": state["block"],
                "delta": {"type": "input_json_delta",
                          "partial_json": "".join(state["pending_args"])},
            }))
            state["pending_args"].clear()
        return out

    def finish() -> list[bytes]:
        nonlocal message_finished
        if message_finished:
            return []
        out: list[bytes] = []
        if not message_started:
            out.extend(start_message({}))
        for index in sorted(tools):
            state = tools[index]
            out.extend(start_tool(state))
        blocks = set()
        if text_block is not None:
            blocks.add(text_block)
        blocks.update(state["block"] for state in tools.values()
                      if state["block"] is not None)
        for block in sorted(blocks):
            out.append(_event("content_block_stop", {
                "type": "content_block_stop", "index": block,
            }))
        out.append(_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }))
        out.append(_event("message_stop", {"type": "message_stop"}))
        message_finished = True
        return out

    def process_frame(frame: bytes) -> list[bytes]:
        nonlocal input_tokens, output_tokens, stop_reason
        data_lines = []
        for line in frame.replace(b"\r\n", b"\n").split(b"\n"):
            if line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            return []
        payload = b"\n".join(data_lines).strip()
        if payload == b"[DONE]":
            return finish()
        try:
            obj = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return []
        if not isinstance(obj, dict):
            return []
        out = start_message(obj)
        usage = obj.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        if "prompt_tokens" in usage:
            input_tokens = _as_nonnegative_int(usage.get("prompt_tokens"))
        if "completion_tokens" in usage:
            output_tokens = _as_nonnegative_int(usage.get("completion_tokens"))
        choices = obj.get("choices") or []
        choice = (choices[0] if isinstance(choices, list) and choices
                  and isinstance(choices[0], dict) else {})
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            delta = {}
        content = delta.get("content")
        text_values = ([content] if isinstance(content, str) else
                       [part.get("text", "") for part in content
                        if isinstance(part, dict) and part.get("type") == "text"]
                       if isinstance(content, list) else [])
        for value in text_values:
            if not value:
                continue
            block, events = start_text()
            out.extend(events)
            out.append(_event("content_block_delta", {
                "type": "content_block_delta", "index": block,
                "delta": {"type": "text_delta", "text": value},
            }))

        for item in delta.get("tool_calls") or []:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index", 0))
            except (ValueError, TypeError):
                index = 0
            state = tools.setdefault(index, {
                "index": index, "id": "", "name": "", "block": None,
                "pending_args": [], "started": False,
            })
            if item.get("id"):
                state["id"] = str(item["id"])
            function = item.get("function") or {}
            if not isinstance(function, dict):
                continue
            if function.get("name"):
                state["name"] += str(function["name"])
            arguments = function.get("arguments")
            if arguments is not None:
                state["pending_args"].append(
                    arguments if isinstance(arguments, str)
                    else json.dumps(arguments, ensure_ascii=False, separators=(",", ":")))
            if state["started"] and arguments is not None:
                out.append(_event("content_block_delta", {
                    "type": "content_block_delta", "index": state["block"],
                    "delta": {"type": "input_json_delta",
                              "partial_json": state["pending_args"].pop()},
                }))
            elif not state["started"] and arguments is not None:
                out.extend(start_tool(state))

        finish_reason = choice.get("finish_reason")
        if finish_reason:
            stop_reason = _STOP_REASONS.get(str(finish_reason), "end_turn")
        return out

    buffer = b""
    for chunk in chunks:
        if not chunk:
            continue
        buffer += chunk
        while True:
            boundary = _split_frame(buffer)
            if boundary is None:
                break
            offset, size = boundary
            frame, buffer = buffer[:offset], buffer[offset + size:]
            yield from process_frame(frame)
            if message_finished:
                return
    if buffer.strip():
        yield from process_frame(buffer)
    yield from finish()


def _as_nonnegative_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError):
        return 0
