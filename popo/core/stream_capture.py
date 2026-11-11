"""Incremental capture of OpenAI-compatible server-sent events."""

from __future__ import annotations

import codecs
import json


class SSECapture:
    """Collect visible text and streamed tool-call fragments across chunks."""

    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = ""
        self._finished = False
        self.text_parts: list[str] = []
        self._tool_calls: dict[int, dict] = {}

    def feed(self, chunk: bytes) -> str:
        if self._finished or not isinstance(chunk, (bytes, bytearray)):
            return ""
        self._pending += self._decoder.decode(bytes(chunk), final=False)
        return self._consume_lines(final=False)

    def finish(self) -> str:
        if self._finished:
            return ""
        self._pending += self._decoder.decode(b"", final=True)
        text = self._consume_lines(final=True)
        self._finished = True
        return text

    @property
    def has_tool_calls(self) -> bool:
        return bool(self._tool_calls)

    def tool_calls(self) -> list[dict]:
        return [self._tool_calls[index] for index in sorted(self._tool_calls)]

    def message(self, content: str) -> dict:
        message = {"role": "assistant", "content": content}
        calls = self.tool_calls()
        if calls:
            message["tool_calls"] = calls
        return message

    def _consume_lines(self, *, final: bool) -> str:
        added: list[str] = []
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            added.extend(self._consume_line(line))
        if final and self._pending:
            added.extend(self._consume_line(self._pending))
            self._pending = ""
        text = "".join(added)
        if text:
            self.text_parts.append(text)
        return text

    def _consume_line(self, line: str) -> list[str]:
        line = line.rstrip("\r")
        if not line.startswith("data:"):
            return []
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return []
        try:
            event = json.loads(payload)
        except (ValueError, TypeError):
            return []
        if not isinstance(event, dict):
            return []
        added: list[str] = []
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            content = delta.get("content") or delta.get("refusal") or ""
            if isinstance(content, str):
                if content:
                    added.append(content)
            elif isinstance(content, list):
                added.extend(str(part.get("text") or "") for part in content
                             if isinstance(part, dict) and part.get("text"))
            for position, fragment in enumerate(delta.get("tool_calls") or []):
                self._merge_tool_call(position, fragment)
        return added

    def _merge_tool_call(self, position: int, fragment: dict) -> None:
        if not isinstance(fragment, dict):
            return
        try:
            index = int(fragment.get("index", position))
        except (TypeError, ValueError):
            index = position
        call = self._tool_calls.setdefault(index, {
            "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
        if fragment.get("id"):
            call["id"] = str(fragment["id"])
        if fragment.get("type"):
            call["type"] = str(fragment["type"])
        function = fragment.get("function") or {}
        if isinstance(function, dict):
            if function.get("name"):
                call["function"]["name"] += str(function["name"])
            arguments = function.get("arguments")
            if arguments is not None:
                call["function"]["arguments"] += (arguments if isinstance(arguments, str)
                                                   else json.dumps(arguments, ensure_ascii=False))
