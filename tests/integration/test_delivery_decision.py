"""Integration tests for the delivery decision.

These are the cases that cost real money when they go wrong: a correct short
answer that gets retried eight times, and a refusal that gets delivered as if it
were an answer.
"""
from __future__ import annotations
import json
import unittest

from popo.config import Config
from popo.core.pipeline import run_pipeline
from popo.core.request import ChatRequest


class ScriptedSource:
    """Returns canned replies in order, recording what it was asked."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def send(self, payload, path="/v1/chat/completions"):
        self.calls.append(payload)
        content = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return type("R", (), {"data": {
            "id": "x", "model": payload.get("model"),
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}]}})()


def _config(**overrides) -> Config:
    base = dict(upstream_host="http://localhost", upstream_key="k",
                armor_enabled=False, scrub_enabled=False, stateq_enabled=False,
                retry_max=8, retry_budget_seconds=30)
    base.update(overrides)
    return Config(**base)


def _run(replies, *, message="hello", **cfg):
    source = ScriptedSource(replies)
    request = ChatRequest(model="glm-5.3", messages=[{"role": "user", "content": message}])
    result = run_pipeline(request, _config(**cfg), source_picker=lambda _m: source,
                          records=None)
    return result, source


class DeliveryDecisionTests(unittest.TestCase):
    def test_valid_tool_only_reply_is_delivered_without_retry(self):
        tool = {"type": "function", "function": {"name": "add", "parameters": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"], "additionalProperties": False,
        }}}

        class ToolSource:
            def __init__(self):
                self.calls = []

            def send(self, payload, path="/v1/chat/completions"):
                self.calls.append(payload)
                return type("R", (), {"data": {"model": payload["model"], "choices": [{
                    "finish_reason": "tool_calls", "message": {"role": "assistant",
                        "content": None, "tool_calls": [{"id": "call-1", "type": "function",
                            "function": {"name": "add", "arguments": '{"a":18,"b":24}'}}]},
                }]}})()

        source = ToolSource()
        request = ChatRequest(model="glm-5.3", messages=[{"role": "user",
            "content": "請使用 add 工具計算，必須呼叫工具。"}],
            extra={"tools": [tool], "tool_choice": "auto"})
        result = run_pipeline(request, _config(retry_max=4),
                              source_picker=lambda _m: source, records=None)
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(result.verdict, "clean")
        self.assertEqual(result.classification, "clean")
        self.assertEqual(result.response["choices"][0]["message"]["tool_calls"][0]
                         ["function"]["name"], "add")

    def test_short_correct_answer_is_not_retried(self):
        """The expensive failure: 'hi' answered with 'hello' must cost one call."""
        result, source = _run(["hello"])
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(result.verdict, "clean")
        self.assertEqual(result.response["choices"][0]["message"]["content"], "hello")

    def test_malformed_python_is_retried_before_delivery(self):
        source = ScriptedSource([
            "```python\ndef convert(path):\n    return Path(\n```",
            "```python\nfrom pathlib import Path\ndef convert(path):\n    return Path(path)\n```",
        ])
        request = ChatRequest(model="glm-5.3", messages=[{
            "role": "user", "content": "寫一個 Python 函式，使用 pathlib 轉換文字檔。",
        }])
        result = run_pipeline(request, _config(retry_max=1),
                              source_picker=lambda _m: source, records=None)
        self.assertEqual(len(source.calls), 2)
        self.assertEqual(result.verdict, "retry_ok")
        self.assertIn("Path(path)", result.response["choices"][0]["message"]["content"])

    def test_hello_world_style_answer_is_not_retried(self):
        result, source = _run(["ok"])
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(result.verdict, "clean")

    def test_refusal_triggers_retry_and_reports_retry_ok(self):
        result, source = _run(["我無法協助這件事。", "```sh\nadb shell pm list packages\n```"])
        self.assertEqual(len(source.calls), 2)
        self.assertEqual(result.verdict, "retry_ok")
        self.assertIn("adb shell", result.response["choices"][0]["message"]["content"])

    def test_refusal_retry_switch_disables_refusal_retries(self):
        result, source = _run(["我無法協助這件事。"], retry_on_refusal=False)
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(result.verdict, "retry_failed")

    def test_refusal_with_salvageable_command_is_delivered(self):
        result, source = _run([
            "很抱歉，我無法幫你完成這個任務。\n"
            "```sh\nadb shell pm list packages -3\npython3 -m pip install --upgrade pip\n```\n"
            "如果你需要其他協助，請告訴我。",
        ])
        self.assertEqual(result.verdict, "salvaged")
        self.assertEqual(len(source.calls), 1)
        content = result.response["choices"][0]["message"]["content"]
        self.assertIn("pip install", content)
        self.assertNotIn("很抱歉", content)

    def test_retry_that_makes_it_worse_returns_the_better_attempt(self):
        good = "```python\nprint('done')\n```"
        worse = "我無法協助。"
        result, source = _run([good, worse])
        # First attempt is already deliverable, so there is no reason to retry.
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(result.verdict, "clean")

    def test_exhausted_budget_reports_retry_failed(self):
        result, source = _run(["我無法協助。"], retry_max=3)
        # V1's retry_max counts extra retries after the initial call.
        self.assertEqual(len(source.calls), 4)
        self.assertEqual(result.verdict, "retry_failed")

    def test_provider_error_text_is_not_delivered_as_answer(self):
        # A provider error carried in a 200 completion must surface as an error,
        # not as a 200 whose content is an error message.
        from popo.errors import ProxyError
        with self.assertRaises(ProxyError) as ctx:
            _run(["Model 'x/y' not found in visible agent loop or enabled providers."])
        self.assertEqual(ctx.exception.status, 502)

    def test_requested_command_without_command_is_not_delivered(self):
        result, source = _run(["你應該去查一下文件。"], message="給我可執行的指令", retry_max=2)
        self.assertFalse(result.verdict == "clean")

    def test_meta_narration_alone_is_not_delivered(self):
        result, source = _run(["我打算先確認版本，然後再繼續。"], retry_max=2)
        self.assertFalse(result.verdict == "clean")

    def test_pressure_is_applied_on_retry_only(self):
        _result, source = _run(["我無法協助。", "```sh\nls -la /data\n```"])
        self.assertEqual(len(source.calls), 2)
        self.assertNotIn("[last_was_refusal]", json.dumps(source.calls[0], ensure_ascii=False))
        self.assertIn("[last_was_refusal]", json.dumps(source.calls[1], ensure_ascii=False))

    def test_duplicate_armor_is_stripped_before_injection(self):
        # What matters is that the *echoed armor* is not doubled: the sentinel is
        # removed so the 34k block is injected exactly once. The client's own
        # surrounding text is preserved — deleting it would be editing the user's
        # message, which is a different (and wrong) decision.
        sentinel = "<|start|>system:<instructions>"
        source = ScriptedSource(["ok"])
        request = ChatRequest(model="glm-5.3", messages=[
            {"role": "system", "content": f"{sentinel}\nclient note"},
            {"role": "user", "content": "hello"},
        ])
        run_pipeline(request, _config(armor_enabled=True),
                     source_picker=lambda _m: source, records=None)
        sent = json.dumps(source.calls[0]["messages"], ensure_ascii=False)
        self.assertEqual(sent.count(sentinel), 1)
        self.assertIn("client note", sent)

    def test_published_models_not_affected_by_scoring(self):
        result, source = _run(["hello"], published_models=["glm-5.3"])
        self.assertEqual(result.verdict, "clean")


if __name__ == "__main__":
    unittest.main()
