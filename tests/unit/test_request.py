"""Unit tests for request normalization."""

import json
import unittest

from popo.core.request import ChatRequest


class TestRequestOpenAI(unittest.TestCase):
    def test_basic(self):
        body = json.dumps({
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hello"}],
        }).encode()
        req = ChatRequest.from_openai(body)
        self.assertEqual(req.model, "glm-5.3")
        self.assertFalse(req.stream)

    def test_stream(self):
        body = json.dumps({
            "model": "deepseek-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "temperature": 0.5,
            "max_tokens": 100,
        }).encode()
        req = ChatRequest.from_openai(body)
        self.assertTrue(req.stream)
        self.assertEqual(req.temperature, 0.5)
        self.assertEqual(req.max_tokens, 100)
        payload = req.to_openai()
        self.assertIn("messages", payload)

    def test_invalid_missing_messages(self):
        body = json.dumps({"model": "x"}).encode()
        from popo.errors import ClientError, ErrorKind
        with self.assertRaises(ClientError) as ctx:
            ChatRequest.from_openai(body)
        self.assertEqual(ctx.exception.kind, ErrorKind.BAD_REQUEST)


class TestRequestAnthropic(unittest.TestCase):
    def test_convert(self):
        body = json.dumps({
            "model": "claude-3-5-sonnet",
            "system": "You are helpful.",
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi"},
            ],
            "max_tokens": 200,
        }).encode()
        req = ChatRequest.from_anthropic(body)
        self.assertEqual(req.messages[0]["role"], "system")
        self.assertEqual(req.messages[1]["role"], "user")
        self.assertEqual(req.max_tokens, 200)


if __name__ == "__main__":
    unittest.main()
