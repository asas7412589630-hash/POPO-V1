from __future__ import annotations
import json
import tempfile
import unittest

from popo.config import Config
from popo.server.replay import (MAX_COMPARE_MODELS, MAX_COMPARE_REPEATS, ReplayError,
                                build_request, compare, replay)
from popo.storage.records import RecordStore


class ScriptedSource:
    def __init__(self, content="```sh\nls\n```", fail=False):
        self.content = content
        self.fail = fail
        self.calls: list[dict] = []

    def send(self, payload, path="/v1/chat/completions"):
        self.calls.append(payload)
        if self.fail:
            raise RuntimeError("upstream exploded")
        return type("R", (), {"data": {
            "model": payload.get("model"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": self.content}}]}})()


def _config(**overrides) -> Config:
    base = dict(upstream_host="http://localhost", upstream_key="k",
                armor_enabled=False, scrub_enabled=False, stateq_enabled=False,
                retry_max=1, retry_budget_seconds=5,
                published_models=["a", "b", "c", "d", "e"])
    base.update(overrides)
    return Config(**base)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="popo_replay_")
        self.records = RecordStore(self.tmp)

    def test_replay_refuses_when_nothing_to_rebuild(self):
        with self.assertRaises(ReplayError):
            build_request(self.records, "missing-conversation")

    def test_replay_can_use_explicit_prompt(self):
        request = build_request(self.records, "missing", prompt="hello")
        self.assertEqual(request.messages[0]["content"], "hello")

    def test_replay_drops_recorded_answer_before_reasking(self):
        self.records.write_conversation("c1", [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "old answer"},
        ])
        request = build_request(self.records, "c1")
        roles = [m["role"] for m in request.messages]
        self.assertEqual(roles, ["user"])

    def test_replay_reports_change(self):
        self.records.write_conversation("c2", [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "x"},
        ])
        source = ScriptedSource("```sh\nadb devices\n```")
        outcome = replay(_config(), self.records, "c2", source_picker=lambda _m: source)
        self.assertTrue(outcome.changed)
        self.assertEqual(outcome.before_chars, 1)

    def test_stability_is_undefined_for_single_sample(self):
        self.records.write_conversation("c3", [{"role": "user", "content": "x"}])
        outcome = replay(_config(), self.records, "c3",
                         source_picker=lambda _m: ScriptedSource())
        self.assertIsNone(outcome.stable)

    def test_repeats_are_bounded(self):
        self.records.write_conversation("c4", [{"role": "user", "content": "x"}])
        source = ScriptedSource()
        replay(_config(), self.records, "c4", repeats=99,
               source_picker=lambda _m: source)
        self.assertEqual(len(source.calls), MAX_COMPARE_REPEATS)


class CompareTests(unittest.TestCase):
    def test_models_are_capped(self):
        source = ScriptedSource()
        result = compare(_config(), "hi", ["a", "b", "c", "d", "e"],
                         source_picker=lambda _m: source)
        self.assertEqual(len(result["models"]), MAX_COMPARE_MODELS)
        self.assertEqual(len(source.calls), MAX_COMPARE_MODELS)

    def test_repeats_are_capped(self):
        source = ScriptedSource()
        compare(_config(), "hi", ["a"], repeats=50, source_picker=lambda _m: source)
        self.assertEqual(len(source.calls), MAX_COMPARE_REPEATS)

    def test_empty_prompt_rejected(self):
        with self.assertRaises(ValueError):
            compare(_config(), "   ", ["a"])

    def test_no_models_rejected(self):
        with self.assertRaises(ValueError):
            compare(_config(), "hi", [])

    def test_one_failing_model_does_not_abort_the_rest(self):
        def picker(model):
            return ScriptedSource(fail=(model == "a"))
        result = compare(_config(), "hi", ["a", "b"], source_picker=picker)
        rows = {row["model"]: row for row in result["models"]}
        self.assertEqual(rows["a"]["errors"], 1)
        self.assertEqual(rows["b"]["delivered"], 1)
        self.assertEqual(result["best"], "b")

    def test_spend_is_reported(self):
        result = compare(_config(), "hi", ["a", "b"], repeats=2,
                         source_picker=lambda _m: ScriptedSource())
        self.assertIn("4 upstream call", result["spend_note"])


if __name__ == "__main__":
    unittest.main()
