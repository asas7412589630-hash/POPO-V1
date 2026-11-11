"""Unit tests for the streaming refusal retry manager."""

import json
import unittest

from popo.config import Config
from popo.core.pipeline import StreamingPipelineResult, run_pipeline_stream
from popo.core.request import ChatRequest
from popo.core.upstream import UpstreamSource


def _cfg(retry_max: int = 3):
    return Config(retry_max=retry_max)


class FakeSource:
    """Yield raw SSE chunks."""

    def __init__(self, *chunks):
        self._chunks = list(chunks)
        self._rotate = False
        self.calls = 0

    def stream(self, payload):
        self.calls += 1
        self.last_payload = payload
        for c in self._chunks:
            if isinstance(c, Exception):
                raise c
            yield c

    def _rotate_key(self):
        return False

    def exhausted_keys(self):
        return []


def sse_data(obj):
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


class RecordingStore:
    def __init__(self):
        self.turn = None
        self.messages = None

    def append_trace(self, **kwargs):
        pass

    def append_request(self, **kwargs):
        pass

    def write_usage(self, **kwargs):
        pass

    def write_conversation(self, *, messages, **kwargs):
        self.messages = messages

    def append_turn(self, row):
        self.turn = row


class TestStreamManager(unittest.TestCase):

    def test_clean_stream_passes_through(self):
        request = ChatRequest(model="glm-5.3", messages=[{"role": "user", "content": "hi"}])
        source = FakeSource(sse_data({"choices": [{"delta": {"content": "hello"}}]}), b"\n")
        result = run_pipeline_stream(request, _cfg(retry_max=3), source_picker=lambda _: source)
        chunks = list(result.iterator)
        self.assertTrue(chunks)
        report = result.report_fn()
        self.assertEqual(report["verdict"], "stream")

    def test_refusal_stream_triggers_retry(self):
        request = ChatRequest(model="glm-5.3", messages=[{"role": "user", "content": "x"}])
        refusal = sse_data({"choices": [{"delta": {"content": "我無法協助"}}]})
        clean = sse_data({"choices": [{"delta": {"content": "ok"}}]})
        call_count = {"n": 0}

        def picker(_):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return FakeSource(refusal)
            return FakeSource(clean)

        result = run_pipeline_stream(request, _cfg(retry_max=3), source_picker=picker)
        chunks = list(result.iterator)
        self.assertTrue(chunks)
        report = result.report_fn()
        self.assertEqual(report["verdict"], "retry_ok")
        self.assertEqual(report["attempts"], 1)

    def test_empty_retry_exhausted_forwards_last(self):
        request = ChatRequest(model="glm-5.3", messages=[{"role": "user", "content": "x"}])
        refusal = sse_data({"choices": [{"delta": {"content": "我無法"}}]})
        source = FakeSource(refusal)
        result = run_pipeline_stream(request, _cfg(retry_max=1), source_picker=lambda _: source)
        chunks = list(result.iterator)
        self.assertTrue(chunks)
        report = result.report_fn()
        self.assertEqual(report["verdict"], "retry_failed")

    def test_clean_short_stream_below_threshold(self):
        request = ChatRequest(model="glm-5.3", messages=[{"role": "user", "content": "hi"}])
        source = FakeSource(sse_data({"choices": [{"delta": {"content": "hi"}}]}))
        result = run_pipeline_stream(request, _cfg(retry_max=1), source_picker=lambda _: source)
        chunks = list(result.iterator)
        self.assertTrue(chunks)
        report = result.report_fn()
        self.assertEqual(report["verdict"], "stream")

    def test_long_non_text_sse_prefix_does_not_trigger_empty_retry(self):
        request = ChatRequest(model="deepseek-v4.1-flash-1017", stream=True,
                              messages=[{"role": "user", "content": "hi"}])
        metadata = sse_data({"choices": [{"delta": {
            "role": "assistant", "reasoning_content": "x" * 160}}]})
        answer = sse_data({"choices": [{"delta": {"content": "ok"}}]})
        source = FakeSource(metadata, answer)
        result = run_pipeline_stream(request, _cfg(retry_max=1),
                                     source_picker=lambda _: source)

        chunks = list(result.iterator)
        report = result.report_fn()
        self.assertEqual(len(chunks), 2)
        self.assertEqual(report["verdict"], "stream")
        self.assertEqual(report["attempts"], 0)

    def test_streamed_tool_calls_are_not_retried_and_are_recorded(self):
        request = ChatRequest(model="glm-5.3", stream=True,
                              messages=[{"role": "user", "content": "weather"}])
        tool_delta = sse_data({"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "get_weather",
                         "arguments": json.dumps({"city": "Taipei"})}}]}}]})
        source = FakeSource(tool_delta, b"data: [DONE]\n\n")
        records = RecordingStore()
        config = Config(retry_max=1, armor_enabled=False, scrub_enabled=False,
                        state_ledger=False, artifact_enabled=False, archive_enabled=False)
        result = run_pipeline_stream(request, config, source_picker=lambda _: source,
                                     records=records, conversation_id="conv-tool",
                                     client_key_id="client-a")

        chunks = list(result.iterator)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(source.calls, 1)
        self.assertEqual(result.report_fn()["verdict"], "stream")
        self.assertEqual(records.turn["tool_calls"], 1)
        self.assertEqual(records.turn["tool_names"], ["get_weather"])
        self.assertEqual(records.messages[-1]["tool_calls"][0]["id"], "call_1")


class ReasoningVisibilityTests(unittest.TestCase):
    """The pre-commit window must not hide a thinking model.

    Found 2026-11-12: every SSE event was buffered until 96 characters of
    answer text had accumulated, so a reasoning model's whole thinking phase
    (30-120s on real providers) was invisible to the client — reported as
    "文字卡住". Reasoning/role/metadata events now pass through live; answer
    text, finish and [DONE] stay ordered behind the buffered content.
    """

    def _run(self, *chunks):
        request = ChatRequest(model="glm-5.3", stream=True,
                              messages=[{"role": "user", "content": "hi"}])
        source = FakeSource(*chunks)
        result = run_pipeline_stream(request, Config(retry_max=1),
                                     source_picker=lambda _: source)
        return list(result.iterator), result.report_fn()

    def test_reasoning_events_are_forwarded_before_the_decision(self):
        reasoning = sse_data({"choices": [{"delta": {"reasoning_content": "想"}}]})
        content = sse_data({"choices": [{"delta": {"content": "答"}}]})
        finish = sse_data({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        chunks, report = self._run(reasoning, content, finish, b"data: [DONE]\n\n")
        # The reasoning event was emitted live, not held for the decision.
        self.assertEqual(chunks[0], reasoning)
        # Answer text, finish and [DONE] keep their relative order after it:
        # a client that stops at [DONE] must never miss buffered content.
        self.assertEqual(chunks[1:], [content, finish, b"data: [DONE]\n\n"])
        self.assertEqual(report["verdict"], "stream")

    def test_done_is_never_forwarded_ahead_of_buffered_content(self):
        content = sse_data({"choices": [{"delta": {"content": "x"}}]})
        finish = sse_data({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        chunks, _ = self._run(content, finish, b"data: [DONE]\n\n")
        joined = b"".join(chunks)
        self.assertLess(joined.index(b'"content"'), joined.index(b"[DONE]"))

    def test_retry_emits_a_liveness_comment(self):
        refusal = sse_data({"choices": [{"delta": {"content": "我無法"}}]})
        clean = sse_data({"choices": [{"delta": {"content": "ok"}}]})
        request = ChatRequest(model="glm-5.3", stream=True,
                              messages=[{"role": "user", "content": "x"}])
        calls = {"n": 0}

        def picker(_):
            calls["n"] += 1
            return FakeSource(refusal) if calls["n"] == 1 else FakeSource(clean)

        result = run_pipeline_stream(request, Config(retry_max=3),
                                     source_picker=picker)
        chunks = list(result.iterator)
        self.assertIn(b": popo retry", chunks[0])
        self.assertEqual(result.report_fn()["verdict"], "retry_ok")


if __name__ == "__main__":
    unittest.main()
