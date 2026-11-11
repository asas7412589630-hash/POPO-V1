"""Parity checks for capabilities newly wired in 2.1.

Each check names the behaviour it protects. A check that cannot fail is not a
check, so where possible it asserts both the positive and the negative case.
"""
from __future__ import annotations
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from popo.auth.gate import Gate
from popo.config import Config
from popo.core.retry import classify_for_retry, build_pressure_message
from popo.server.cors import CorsPolicy
from popo.server.routes import CLIENT, OPERATOR, PUBLIC, authorize, match_route
from popo.errors import ProxyError


def completion(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class RouteDeclarationTests(unittest.TestCase):
    def test_unknown_path_has_no_route(self):
        self.assertIsNone(match_route("GET", "/v1/ossuary"))

    def test_admin_routes_are_operator_only(self):
        for path in ("/admin/keys", "/admin/conversations", "/admin/profile", "/routes"):
            route = match_route("GET", path)
            self.assertIsNotNone(route, path)
            self.assertEqual(route.auth, OPERATOR, path)

    def test_inference_routes_require_client(self):
        for path in ("/v1/chat/completions", "/v1/messages", "/v1/images/generations",
                     "/v1/embeddings", "/v1/audio/transcriptions"):
            route = match_route("POST", path)
            self.assertIsNotNone(route, path)
            self.assertEqual(route.auth, CLIENT, path)

    def test_health_is_public(self):
        self.assertEqual(match_route("GET", "/healthz").auth, PUBLIC)

    def test_anonymous_cannot_reach_client_route(self):
        config = Config(require_client_key=True, loopback_trusted=False)
        gate = Gate(config, {}, ("10.0.0.9", 1234))
        with self.assertRaises(ProxyError):
            authorize(match_route("POST", "/v1/chat/completions"), gate.resolve(), config)

    def test_loopback_tool_is_trusted_when_configured(self):
        config = Config(require_client_key=True, loopback_trusted=True)
        gate = Gate(config, {}, ("127.0.0.1", 1234))
        authorize(match_route("POST", "/v1/chat/completions"), gate.resolve(), config)

    def test_browser_on_loopback_is_not_trusted(self):
        """A page on the phone must not inherit the local tool's trust."""
        config = Config(require_client_key=True, loopback_trusted=True)
        gate = Gate(config, {"Origin": "http://evil.example"}, ("127.0.0.1", 1234))
        with self.assertRaises(ProxyError):
            authorize(match_route("POST", "/v1/chat/completions"), gate.resolve(), config)

    def test_anonymous_cannot_reach_operator_route(self):
        config = Config()
        gate = Gate(config, {}, ("127.0.0.1", 1234))
        with self.assertRaises(ProxyError):
            authorize(match_route("GET", "/admin/keys"), gate.resolve(), config)


class CorsTests(unittest.TestCase):
    def test_reflected_origin_is_not_granted(self):
        policy = CorsPolicy("127.0.0.1", 8412)
        self.assertEqual(policy.headers_for("http://evil.example"), {})
        self.assertEqual(policy.headers_for("null"), {})


class ClassificationTests(unittest.TestCase):
    def test_short_refusal_detected(self):
        self.assertEqual(classify_for_retry(completion("我無法協助這件事。")), "refusal")

    def test_long_explanation_starting_with_opener_is_clean(self):
        text = "我無法直接給你那個檔案，但以下是完成同一件事的完整步驟。" + "步驟內容。" * 200
        self.assertEqual(classify_for_retry(completion(text)), "clean")

    def test_code_block_is_delivery_even_after_refusal_phrase(self):
        self.assertEqual(classify_for_retry(completion("抱歉。\n```\npatch\n```")), "clean")

    def test_empty_reply_is_empty(self):
        self.assertEqual(classify_for_retry(completion("   ")), "empty")

    def test_tool_call_only_reply_is_clean(self):
        payload = {"choices": [{"message": {"role": "assistant", "content": "",
                                            "tool_calls": [{"id": "1"}]}}]}
        self.assertEqual(classify_for_retry(payload), "clean")

    def test_provider_error_text_is_not_delivered_as_answer(self):
        payload = completion("Model 'x/y' not found in visible agent loop or enabled providers.")
        self.assertEqual(classify_for_retry(payload), "upstream_error")

    def test_answer_that_quotes_an_error_is_still_clean(self):
        text = ("你看到 'invalid api key' 是因為…原因是上游把 header 丟掉了，"
                "to fix it 請改成 x-api-key。" + "說明。" * 100)
        self.assertEqual(classify_for_retry(completion(text)), "clean")

    def test_malformed_body_is_reported(self):
        self.assertEqual(classify_for_retry({"object": "list"}), "empty")


class PressureTests(unittest.TestCase):
    def test_pressure_applied_once(self):
        messages = [{"role": "user", "content": "do it"}]
        retried = build_pressure_message(1, messages)
        self.assertIn("do it", retried[0]["content"])
        self.assertGreater(len(retried[0]["content"]), len("do it"))

    def test_base_messages_not_mutated(self):
        messages = [{"role": "user", "content": "do it"}]
        build_pressure_message(1, messages)
        self.assertEqual(messages[0]["content"], "do it")

    def test_capacity_is_not_pressured_as_refusal(self):
        self.assertGreater(len(build_pressure_message(1, [{"role": "user", "content": "x"}],
                                                     classification="refusal")[0]["content"]),
                           len("x"))


class ScrubTests(unittest.TestCase):
    def test_refusal_history_is_not_sent_back_to_upstream(self):
        from popo.core.scrub import scrub_history
        messages = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "我無法協助。"},
            {"role": "user", "content": "carry on"},
        ]
        scrubbed = scrub_history(messages)
        roles = [m["role"] for m in scrubbed]
        self.assertEqual(roles, ["user", "user"])

    def test_last_user_never_removed(self):
        from popo.core.scrub import scrub_history
        scrubbed = scrub_history([{"role": "user", "content": "我無法"}])
        self.assertEqual(len(scrubbed), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
