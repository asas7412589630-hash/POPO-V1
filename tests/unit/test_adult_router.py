"""Unit tests for adult router."""

import json
import unittest

from popo.adult.router import AdultRouter
from popo.config import Config
from popo.errors import ClientError, ErrorKind


class FakeSource:
    def __init__(self, content, verdict="clean", attempts=0):
        self.content = content
        self.verdict = verdict
        self.attempts = attempts

    def send(self, payload):
        self.last_payload = payload
        return type("R", (), {"data": {
            "id": "up-1",
            "choices": [{"message": {"role": "assistant", "content": self.content}}],
        }})()


class TestAdultRouter(unittest.TestCase):
    def test_disabled_raises_not_available(self):
        router = AdultRouter(Config(adult_enabled=False), lambda _: None)
        with self.assertRaises(ClientError) as ctx:
            router.dispatch("/v1/adult/chat", b'{"messages": []}')
        self.assertIn("disabled", ctx.exception.message)
        self.assertEqual(ctx.exception.kind, ErrorKind.PROXY_NOT_AVAILABLE)

    def test_chat_returns_openai_shape(self):
        source = FakeSource("ok")
        router = AdultRouter(Config(adult_enabled=True), lambda _: source)
        body = json.dumps({"model": "glm-5.3", "messages": [{"role": "user", "content": "x"}]}).encode()
        result = router.dispatch("/v1/adult/chat", body)
        self.assertEqual(result["object"], "chat.completion")
        self.assertIn("glm", result["model"])
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")

    def test_unknown_route_404(self):
        router = AdultRouter(Config(adult_enabled=True), lambda _: None)
        with self.assertRaises(ClientError) as ctx:
            router.dispatch("/v1/adult/video", b'{}')
        self.assertEqual(ctx.exception.kind.value, "client.not_found")


if __name__ == "__main__":
    unittest.main()
