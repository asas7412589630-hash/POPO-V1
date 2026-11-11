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
        # Providers report why generation stopped in the last chunk. Nothing
        # recorded it before, so a stream cut off by max_tokens looked exactly
        # like a complete answer that happened to end mid-sentence.
        self.finish_reason: str = ""
        # See _consume_line: [DONE] means no more chunks are coming, and a
        # provider that holds the socket open afterwards must not hang the caller.
        self.done: bool = False
        self.text_parts: list[str] = []
        # Thinking is not an answer, but it proves the model is producing
        # something. A stream that opens with reasoning must not be mistaken for
        # an empty completion and retried.
        self.reasoning_parts: list[str] = []
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
    def has_reasoning(self) -> bool:
        return bool(self.reasoning_parts)

    @property
    def truncated(self) -> bool:
        """The provider stopped because the output budget ran out."""
        return self.finish_reason == "length"

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

    def _note_finish(self, choice: dict) -> None:
        reason = choice.get("finish_reason")
        if reason:
            self.finish_reason = str(reason)

    def _consume_line(self, line: str) -> list[str]:
        line = line.rstrip("\r")
        if not line.startswith("data:"):
            return []
        payload = line[5:].strip()
        if not payload:
            return []
        if payload == "[DONE]":
            # The stream is complete. Record it: providers that keep the socket
            # open after [DONE] would otherwise leave the caller waiting on a
            # read that will never return, which presents as a hang rather than
            # a finished answer (measured 2026-11-11: 151 chunks, then 120s of
            # silence until the client timed out).
            self.done = True
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
            self._note_finish(choice)
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
            # Reasoning arrives as its own delta field, not as content. Without
            # this the opening of a reasoning stream looks like "no text at all",
            # which the pipeline classifies as an empty answer and retries — so
            # one question cost two full generations and the client received two
            # [DONE] markers (measured 2026-11-11 on glm-5.3).
            for key in ("reasoning", "reasoning_content"):
                piece = delta.get(key)
                if isinstance(piece, str) and piece:
                    self.reasoning_parts.append(piece)
                    break
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
