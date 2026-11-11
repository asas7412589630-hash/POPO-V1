"""Anthropic Messages request, response, and SSE compatibility tests."""

from __future__ import annotations

import json
import unittest

from popo.core.request import ChatRequest
from popo.server.anthropic_sse import iter_anthropic_events
from popo.server.endpoints import to_anthropic_message


def _decode_events(raw: bytes) -> list[tuple[str, dict]]:
    events = []
    for frame in raw.decode("utf-8").strip().split("\n\n"):
        lines = frame.splitlines()
        name = next((line[7:] for line in lines if line.startswith("event: ")), "message")
        data = next((line[6:] for line in lines if line.startswith("data: ")), "{}")
        events.append((name, json.loads(data)))
    return events


class AnthropicRequestTests(unittest.TestCase):
    def test_tool_use_result_schema_choice_and_stop_sequences_are_normalized(self):
        req = ChatRequest.from_anthropic(json.dumps({
            "model": "glm-5.3",
            "system": [{"type": "text", "text": "Be concise."}],
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "text", "text": "Checking."},
                    {"type": "tool_use", "id": "call-1", "name": "weather",
                     "input": {"city": "Taipei"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "call-1", "content": "Sunny"},
                    {"type": "text", "text": "Continue."},
                ]},
            ],
            "tools": [{"name": "weather", "description": "Get weather",
                       "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}],
            "tool_choice": {"type": "tool", "name": "weather"},
            "stop_sequences": ["END"],
            "max_tokens": 120,
        }).encode())

        payload = req.to_openai()
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "Be concise."})
        self.assertEqual(payload["messages"][1]["tool_calls"][0]["function"], {
            "name": "weather", "arguments": '{"city":"Taipei"}',
        })
        self.assertEqual(payload["messages"][2]["role"], "tool")
        self.assertEqual(payload["messages"][2]["tool_call_id"], "call-1")
        self.assertEqual(payload["messages"][3]["content"], "Continue.")
        self.assertEqual(payload["tools"][0]["function"]["name"], "weather")
        self.assertEqual(payload["tool_choice"]["function"]["name"], "weather")
        self.assertEqual(payload["stop"], ["END"])

    def test_base64_image_is_preserved(self):
        req = ChatRequest.from_anthropic(json.dumps({
            "model": "glm-5.3", "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Describe."},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                  "data": "aGVsbG8="}},
            ]}],
        }).encode())
        content = req.to_openai()["messages"][0]["content"]
        self.assertEqual(content[0]["text"], "Describe.")
        self.assertEqual(content[1]["image_url"]["url"], "data:image/png;base64,aGVsbG8=")

    def test_tool_result_image_is_preserved(self):
        req = ChatRequest.from_anthropic(json.dumps({
            "model": "glm-5.3", "messages": [{"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call-1", "content": [
                    {"type": "text", "text": "Screenshot"},
                    {"type": "image", "source": {"type": "base64",
                        "media_type": "image/png", "data": "aGVsbG8="}},
                ]},
            ]}],
        }).encode())
        message = req.to_openai()["messages"][0]
        self.assertEqual(message["role"], "tool")
        self.assertEqual(message["content"][0]["text"], "Screenshot")
        self.assertEqual(message["content"][1]["image_url"]["url"],
                         "data:image/png;base64,aGVsbG8=")


class AnthropicResponseTests(unittest.TestCase):
    def test_tool_call_is_returned_as_tool_use_block(self):
        response = to_anthropic_message({
            "id": "cmpl-1", "model": "glm-5.3",
            "choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "Checking.",
                "tool_calls": [{"id": "call-1", "type": "function",
                                "function": {"name": "weather", "arguments": '{"city":"Taipei"}'}}],
            }}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 4},
        })
        self.assertEqual(response["stop_reason"], "tool_use")
        self.assertEqual(response["content"][1], {
            "type": "tool_use", "id": "call-1", "name": "weather", "input": {"city": "Taipei"},
        })


class AnthropicSSETests(unittest.TestCase):
    def test_fragmented_text_and_tool_stream_has_complete_anthropic_lifecycle(self):
        records = [
            {"id": "chatcmpl-1", "model": "glm-5.3", "choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1",
                "type": "function", "function": {"name": "weath"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0,
                "function": {"name": "er", "arguments": '{"city":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0,
                "function": {"arguments": '"Taipei"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 8, "completion_tokens": 6}},
        ]
        wire = b"".join(b"data: " + json.dumps(row).encode() + b"\r\n\r\n" for row in records)
        wire += b"data: [DONE]\r\n\r\n"
        # Deliberately split every framing boundary across arbitrary chunks.
        output = b"".join(iter_anthropic_events((wire[i:i + 3] for i in range(0, len(wire), 3))))
        events = _decode_events(output)
        names = [name for name, _ in events]
        self.assertEqual(names[0], "message_start")
        self.assertEqual(names.count("content_block_start"), 2)
        self.assertEqual(names.count("content_block_stop"), 2)
        self.assertEqual(names[-2:], ["message_delta", "message_stop"])
        self.assertNotIn("[DONE]", output.decode())
        block = next(payload["content_block"] for name, payload in events
                     if name == "content_block_start" and payload["content_block"]["type"] == "tool_use")
        self.assertEqual((block["id"], block["name"]), ("call-1", "weather"))
        partial = "".join(payload["delta"]["partial_json"] for name, payload in events
                          if name == "content_block_delta"
                          and payload["delta"]["type"] == "input_json_delta")
        self.assertEqual(partial, '{"city":"Taipei"}')
        self.assertEqual(events[-2][1]["delta"]["stop_reason"], "tool_use")
        self.assertEqual(events[-2][1]["usage"], {"input_tokens": 8, "output_tokens": 6})


if __name__ == "__main__":
    unittest.main()
