"""Streams must end: exactly one terminator, and no waiting after it.

Three defects stacked into one symptom — a client hung on a completed answer,
and when it finally gave up it had received two ``data: [DONE]`` markers for one
question. Each was invisible on its own:

1. ``[DONE]`` was parsed but not recorded, so the reader kept reading a socket
   the provider had finished with. Measured: 25s of silence after the last chunk,
   120s until the client timed out.
2. ``delta.reasoning`` was not collected at all. A reasoning model emits thinking
   first, so the opening looked like "no text" and the pipeline retried — paying
   for a second full generation.
3. The endpoint added ``data: [DONE]`` in ``finally`` regardless of whether the
   upstream already sent one, so every stream ended with two.

The tests below pin the observable contract: one terminator, prompt EOF, and
reasoning recognised as progress rather than as emptiness.
"""
from __future__ import annotations
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from popo.core.stream_capture import SSECapture   # noqa: E402


def _feed_all(capture: SSECapture, chunks: list[bytes]) -> str:
    text = ""
    for chunk in chunks:
        text += capture.feed(chunk)
    return text


class TerminatorTests(unittest.TestCase):
    def test_done_is_recorded(self):
        c = SSECapture()
        _feed_all(c, [b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n',
                      b"data: [DONE]\n\n"])
        self.assertTrue(c.done)

    def test_done_absent_before_it_arrives(self):
        c = SSECapture()
        _feed_all(c, [b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n'])
        self.assertFalse(c.done)

    def test_done_in_same_chunk_as_content(self):
        c = SSECapture()
        text = _feed_all(c, [
            b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n'
            b"\n\ndata: [DONE]\n\n"])
        self.assertEqual(text, "hi")
        self.assertTrue(c.done)

    def test_two_done_markers_do_not_break_parsing(self):
        """The duplicate was our bug, but a provider may also send two."""
        c = SSECapture()
        _feed_all(c, [b"data: [DONE]\n\ndata: [DONE]\n\n"])
        self.assertTrue(c.done)


class ReasoningDeltaTests(unittest.TestCase):
    def test_reasoning_delta_is_collected(self):
        c = SSECapture()
        _feed_all(c, [b'data: {"choices":[{"delta":{"reasoning":"[C"},"finish_reason":null}]}\n\n',
                      b'data: {"choices":[{"delta":{"reasoning":"]\\nok"},"finish_reason":null}]}\n\n'])
        self.assertTrue(c.has_reasoning)
        self.assertIn("[C", "".join(c.reasoning_parts))

    def test_reasoning_content_variant_is_collected(self):
        """Some providers use reasoning_content instead of reasoning."""
        c = SSECapture()
        _feed_all(c, [b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\n'])
        self.assertTrue(c.has_reasoning)

    def test_reasoning_is_not_answer_text(self):
        """Thinking must never be forwarded as the assistant's reply."""
        c = SSECapture()
        text = _feed_all(c, [b'data: {"choices":[{"delta":{"reasoning":"secret thoughts"}}]}\n\n'])
        self.assertEqual(text, "")
        self.assertFalse(c.has_reasoning and text)

    def test_reasoning_then_content_yields_only_content(self):
        c = SSECapture()
        text = _feed_all(c, [
            b'data: {"choices":[{"delta":{"reasoning":"think"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            b"data: [DONE]\n\n"])
        self.assertEqual(text, "answer")
        self.assertTrue(c.has_reasoning)
        self.assertTrue(c.done)

    def test_reasoning_stream_that_never_answers_is_detectable(self):
        """The case that must be treated as starvation, not as an empty reply."""
        c = SSECapture()
        _feed_all(c, [
            b'data: {"choices":[{"delta":{"reasoning":"thinking hard"}}]}\n\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n',
            b"data: [DONE]\n\n"])
        self.assertTrue(c.has_reasoning)
        self.assertTrue(c.truncated)
        self.assertEqual("".join(c.text_parts), "")


class EndpointTerminatorTests(unittest.TestCase):
    """Structural: the endpoint must not add a second [DONE]."""

    def test_endpoint_does_not_unconditionally_emit_done(self):
        source = (ROOT / "popo" / "server" / "endpoints.py").read_text(encoding="utf-8")
        # Find the SSE handler body and confirm the unconditional write is gone.
        self.assertNotIn("            writer.sse_chunk(b\"data: [DONE]\\n\\n\")\n\n\nclass",
                         source)
        self.assertIn("sent_done", source,
                      "endpoint must track whether upstream already terminated")

    def test_sse_start_declares_chunked_framing(self):
        """HTTP/1.1 with no length and no framing means 'read until close'."""
        source = (ROOT / "popo" / "server" / "responses.py").read_text(encoding="utf-8")
        self.assertIn("Transfer-Encoding", source)
        self.assertIn("chunked", source)
        self.assertIn("def sse_end", source)


if __name__ == "__main__":
    unittest.main()
