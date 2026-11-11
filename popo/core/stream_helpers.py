"""Streaming transport helpers shared by the pipeline.

The pipeline module holds the retry state machine; these are the byte-level
utilities it needs to read, classify and abort an upstream SSE stream. Kept in
their own module because pipeline.py sits against the 700-line architecture
ceiling (same reason 2.7.12 moved its streaming helpers out).
"""

from __future__ import annotations

import json

from popo.errors import ProxyError

# Emitted before a pre-commit retry. An SSE comment line: every compliant
# parser ignores it, and it proves the connection is alive while the proxy
# discards a refused attempt and starts another one.
RETRY_MARK = b": popo retry\n\n"


def content_free_chunk(chunk: bytes) -> bool:
    """True when a transport chunk carries no answer text at all.

    Reasoning deltas, role announcements and metadata may reach the client
    before the pre-commit decision: the refusal judgement reads only content
    text, so forwarding them cannot leak a refused opening. A chunk whose
    trailing line is incomplete, or whose JSON cannot be parsed, is treated as
    content-bearing (buffered) — the conservative side of both failures.
    """
    if not chunk.endswith(b"\n"):
        return False
    text = chunk.decode("utf-8", "replace")
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        # [DONE] terminates the stream in every client: it must never be
        # forwarded ahead of buffered answer text, so it counts as
        # content-bearing here.
        if not payload:
            continue
        if payload == "[DONE]":
            return False
        try:
            event = json.loads(payload)
        except ValueError:
            return False
        if not isinstance(event, dict):
            return False
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                return False
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                return False
            if delta.get("content") or delta.get("refusal"):
                return False
            if delta.get("tool_calls"):
                return False
    return True


def read_chunk(stream) -> bytes:
    """Return the next chunk from a stream handle or an iterator.

    ``UpstreamStream`` exposes ``read()`` so a caller can stop early without the
    generator ``finally`` closing the socket. Test doubles and alternative
    transports may only be iterators, so both shapes are accepted here — at the
    boundary, once, rather than scattered ``hasattr`` checks.
    """
    reader = getattr(stream, "read", None)
    if reader is not None:
        return reader()
    try:
        return next(stream)
    except StopIteration:
        return b""


def abort_stream(stream) -> None:
    """Stop an upstream stream without letting its failure surface as an error."""
    abort = getattr(stream, "abort", None)
    if abort is None:
        close = getattr(stream, "close", None)
        if close:
            try:
                close()
            except (OSError, ProxyError):
                pass
        return
    try:
        abort()
    except (OSError, ProxyError):
        pass
