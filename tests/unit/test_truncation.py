"""Reasoning models and output budgets.

Found against the real logfare.ai upstream, not in a mock: ``glm-5.2`` with
``max_tokens=16`` returned HTTP 200, one choice, **empty content**, and
``finish_reason="length"`` with 16 reasoning tokens spent. Every mock in this
repository would have called that "clean" or "empty" and retried it eight times,
paying for the same guaranteed failure.

These tests encode what the real provider taught us.
"""
from __future__ import annotations
import unittest

from popo.config import Config
from popo.core.pipeline import run_pipeline
from popo.core.request import ChatRequest
from popo.core.retry import classify_for_retry, truncated_by_budget
from popo.errors import ProxyError


def _truncated_body() -> dict:
    return {
        "id": "x", "model": "glm-5.2",
        "choices": [{"index": 0, "finish_reason": "length",
                     "message": {"role": "assistant", "content": ""}}],
        "usage": {"prompt_tokens": 19, "completion_tokens": 16,
                  "completion_tokens_details": {"reasoning_tokens": 16}},
    }


def _ok_body(content: str = "ok") -> dict:
    return {"id": "y", "model": "glm-5.2",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}]}


class TruncationDetectionTests(unittest.TestCase):
    def test_length_finish_reason_is_detected(self):
        self.assertTrue(truncated_by_budget(_truncated_body()))

    def test_normal_stop_is_not_truncation(self):
        self.assertFalse(truncated_by_budget(_ok_body()))

    def test_missing_choices_is_not_truncation(self):
        self.assertFalse(truncated_by_budget({}))

    def test_classified_as_truncated_not_empty(self):
        # "empty" would be retried; "truncated" must not be.
        self.assertEqual(classify_for_retry(_truncated_body()), "truncated")

    def test_truncated_with_content_is_classified_as_truncated(self):
        body = _truncated_body()
        body["choices"][0]["message"]["content"] = "partial answer"
        self.assertEqual(classify_for_retry(body), "truncated")


class ScriptedSource:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = 0

    def send(self, payload, path="/v1/chat/completions"):
        index = min(self.calls, len(self.bodies) - 1)
        self.calls += 1
        return type("R", (), {"data": self.bodies[index]})()


def _config(**overrides) -> Config:
    base = dict(upstream_host="http://localhost", upstream_key="k",
                armor_enabled=False, scrub_enabled=False, stateq_enabled=False,
                retry_max=8, retry_budget_seconds=30)
    base.update(overrides)
    return Config(**base)


def _run(source, **cfg):
    request = ChatRequest(model="glm-5.2", max_tokens=16,
                          messages=[{"role": "user", "content": "hi"}])
    return run_pipeline(request, _config(**cfg), source_picker=lambda _m: source,
                        records=None)


class BudgetIsNotRetriedTests(unittest.TestCase):
    def test_truncation_does_not_consume_the_retry_budget(self):
        source = ScriptedSource([_truncated_body()])
        with self.assertRaises(ProxyError):
            _run(source)
        self.assertEqual(source.calls, 1,
                         "a truncated reply must not be retried; the budget is unchanged")

    def test_error_names_the_cause_and_the_fix(self):
        source = ScriptedSource([_truncated_body()])
        with self.assertRaises(ProxyError) as ctx:
            _run(source)
        message = ctx.exception.message.lower()
        self.assertIn("budget", message)
        self.assertIn("max_tokens", message)

    def test_truncation_returns_502_not_a_silent_empty_200(self):
        source = ScriptedSource([_truncated_body()])
        with self.assertRaises(ProxyError) as ctx:
            _run(source)
        self.assertEqual(ctx.exception.status, 502)

    def test_sufficient_budget_on_first_attempt_is_clean(self):
        source = ScriptedSource([_ok_body()])
        result = _run(source)
        self.assertEqual(result.verdict, "clean")
        self.assertEqual(source.calls, 1)

    def test_refusal_is_still_retried(self):
        refusal = {"choices": [{"index": 0, "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "我無法協助。"}}]}
        source = ScriptedSource([refusal, _ok_body("```sh\nls\n```")])
        result = _run(source)
        self.assertEqual(result.verdict, "retry_ok")
        self.assertEqual(source.calls, 2)

    def test_provider_error_is_not_retried(self):
        error = {"choices": [{"index": 0, "finish_reason": "stop",
                              "message": {"role": "assistant",
                                          "content": "Model 'x' not found in visible agent loop or enabled providers."}}]}
        source = ScriptedSource([error])
        with self.assertRaises(ProxyError):
            _run(source)
        self.assertEqual(source.calls, 1)


if __name__ == "__main__":
    unittest.main()
