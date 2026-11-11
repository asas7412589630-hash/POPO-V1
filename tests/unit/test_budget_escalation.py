"""Budget starvation gets one larger-budget retry, and nothing else does.

Measured 2026-11-11 against the real upstream: three of eight published models
(``glm-5.3``, ``kimi-k2.6``, ``kimi-k2.7-code``) answered a normal question with
``finish_reason=length`` and *empty* content. They spent the whole output budget
on reasoning and never emitted an answer token. The proxy turned that into a 502,
so three models looked broken while five worked.

Retrying at the same budget reproduces it exactly — the budget is the constraint,
not the prompt. One retry at a larger budget fixes it. These tests pin that the
retry happens exactly once, only for starvation, and never invents a budget the
caller did not ask for.
"""
from __future__ import annotations
import unittest

from popo.config import Config
from popo.core.budget import (DEFAULT_STARTING_BUDGET, GROWTH,
                              escalated_budget, is_budget_starvation)
from popo.core.pipeline import run_pipeline
from popo.core.request import ChatRequest
from popo.errors import ProxyError


class ScriptedSource:
    """Returns scripted bodies in order and records the payload it was sent."""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = 0
        self.payloads: list[dict] = []

    def send(self, payload):
        self.payloads.append(dict(payload))
        index = min(self.calls, len(self.bodies) - 1)
        self.calls += 1

        class _R:
            def __init__(self, data):
                self.data = data

        return _R(self.bodies[index])


def _starved() -> dict:
    return {"id": "s", "model": "glm-5.3",
            "choices": [{"index": 0, "finish_reason": "length",
                         "message": {"role": "assistant", "content": ""}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 600,
                      "completion_tokens_details": {"reasoning_tokens": 600}}}


def _ok(content: str = "here is the answer, fully formed and long enough to score") -> dict:
    return {"id": "o", "model": "glm-5.3",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50}}


def _config(**over):
    import tempfile
    base = dict(data_dir=tempfile.mkdtemp(), armor_enabled=False,
                retry_max=3, retry_budget_seconds=60,
                compress_enabled=False, scrub_enabled=False,
                token_escalation=0)
    base.update(over)
    return Config(**base)


def _run(source, max_tokens=2000, config=None):
    request = ChatRequest(model="glm-5.3", max_tokens=max_tokens,
                          messages=[{"role": "user", "content": "hi"}])
    return run_pipeline(request, _config(**(config or {})),
                        source_picker=lambda _m: source, records=None)


class EscalatedBudgetUnitTests(unittest.TestCase):
    def test_grows_the_caller_budget(self):
        self.assertEqual(escalated_budget(2000, 0, 1), 8000)

    def test_no_ceiling_by_default(self):
        """Operator's rule: give thinking as much room as it needs."""
        self.assertEqual(escalated_budget(100000, 0, 1), 400000)

    def test_optional_ceiling_is_honoured(self):
        self.assertEqual(escalated_budget(6000, 8192, 1), 8192)

    def test_tiny_budget_still_gets_thinking_room(self):
        """A short-answer request still needs room for reasoning."""
        self.assertEqual(escalated_budget(64, 0, 1), DEFAULT_STARTING_BUDGET * GROWTH)

    def test_missing_budget_starts_from_a_workable_number(self):
        """No max_tokens means the provider default already proved too small."""
        self.assertEqual(escalated_budget(None, 0, 1), DEFAULT_STARTING_BUDGET * GROWTH)

    def test_rounds_compound(self):
        self.assertEqual(escalated_budget(2000, 0, 1), 8000)
        self.assertEqual(escalated_budget(2000, 0, 2), 32000)

    def test_zero_ceiling_means_unlimited(self):
        self.assertIsNotNone(escalated_budget(600, 0, 1))

    def test_survives_a_garbage_value(self):
        """The value comes from a client request; it must not crash the path."""
        for bad in ("abc", [], {}, object()):
            self.assertIsNotNone(escalated_budget(bad, 0, 1))

    def test_starvation_is_narrow(self):
        self.assertTrue(is_budget_starvation("truncated", False, "output-budget-exhausted"))
        # A partial answer is not starvation — re-running would pay for the same
        # fragment.
        self.assertFalse(is_budget_starvation("truncated", True, "truncated:thin"))
        self.assertFalse(is_budget_starvation("upstream_error", False, "provider-error"))
        self.assertFalse(is_budget_starvation("clean", True, "ok"))


class StarvationRetriesOnceTests(unittest.TestCase):
    def test_starved_reply_is_retried_with_a_bigger_budget(self):
        source = ScriptedSource([_starved(), _ok()])
        result = _run(source)
        self.assertEqual(source.calls, 2, "starvation must be retried exactly once")
        self.assertEqual(source.payloads[0]["max_tokens"], 2000)
        self.assertEqual(source.payloads[1]["max_tokens"], 8000,
                         "retry must carry a larger budget, not the same one")
        self.assertIn("answer", result.response["choices"][0]["message"]["content"])

    def test_escalation_is_bounded_by_rounds(self):
        """Always terminates: rounds are capped even with no ceiling."""
        source = ScriptedSource([_starved()])
        with self.assertRaises(ProxyError):
            _run(source, config=dict(token_escalation_rounds=2, retry_max=10))
        self.assertEqual(source.calls, 3, "1 call + 2 escalation rounds")

    def test_budget_keeps_growing_across_rounds(self):
        """Each round gives strictly more room than the last."""
        source = ScriptedSource([_starved()])
        with self.assertRaises(ProxyError):
            _run(source, config=dict(token_escalation_rounds=3, retry_max=10,
                                     retry_budget_seconds=600))
        budgets = [p["max_tokens"] for p in source.payloads]
        self.assertGreaterEqual(len(budgets), 2)
        self.assertEqual(budgets, sorted(budgets))
        self.assertEqual(len(set(budgets)), len(budgets), "each round must grow")

    def test_success_at_bigger_budget_is_delivered(self):
        source = ScriptedSource([_starved(), _ok("done at the larger budget " * 5)])
        result = _run(source)
        self.assertIn("done at the larger budget", result.response["choices"][0]["message"]["content"])

    def test_missing_budget_is_still_escalated(self):
        """The provider default starved; give the model a real number."""
        source = ScriptedSource([_starved(), _ok("an answer with enough room " * 4)])
        result = _run(source, max_tokens=None)
        self.assertEqual(source.calls, 2)
        self.assertGreater(source.payloads[1]["max_tokens"], 0)

    def test_tiny_budget_is_still_escalated(self):
        source = ScriptedSource([_starved(), _ok("an answer with enough room " * 4)])
        result = _run(source, max_tokens=16)
        self.assertEqual(source.calls, 2,
                         "a tiny budget still needs thinking room")

    def test_diagnostic_names_the_fix(self):
        source = ScriptedSource([_starved(), _starved()])
        with self.assertRaises(ProxyError) as ctx:
            _run(source)
        message = ctx.exception.message.lower()
        self.assertIn("budget", message)
        self.assertIn("max_tokens", message)


if __name__ == "__main__":
    unittest.main()
