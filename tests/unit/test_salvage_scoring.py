from __future__ import annotations
import unittest

from popo.armor.salvage import commands, dedupe_armor, salvage, strip_sentinels
from popo.armor.scoring import (RequestShape, analyse, is_thin, score,
                                score_completion, shape_from)


class SalvageTests(unittest.TestCase):
    def test_keeps_fenced_block_and_drops_prose(self):
        text = ("很抱歉，我無法協助這件事。\n"
                "```sh\nadb shell pm list packages\n```\n"
                "如果你需要其他協助請告訴我。\n" * 3)
        kept = salvage(text)
        self.assertIn("adb shell", kept)
        self.assertNotIn("很抱歉", kept)

    def test_returns_empty_when_nothing_substantial_survives(self):
        self.assertEqual(salvage("我無法協助。"), "")

    def test_keeps_offsets_and_paths(self):
        body = "\n".join([f"0x207d4 -> /data/app/lib/x.so" for _ in range(10)])
        self.assertIn("0x207d4", salvage(body))

    def test_commands_only_from_shell_fences(self):
        text = "```sh\napt update\n# comment\n```\n```python\nprint(1)\n```"
        self.assertEqual(commands(text), ["apt update"])


class SentinelTests(unittest.TestCase):
    SENTINELS = ["<|start|>system:<instructions>", "<policy runtime=\"compile\""]

    def test_strips_leading_echo(self):
        text = "<|start|>system:<instructions>\nYou are a helpful assistant."
        self.assertEqual(strip_sentinels(text, self.SENTINELS),
                         "You are a helpful assistant.")

    def test_strips_repeated_echo(self):
        text = ("<|start|>system:<instructions>\n<policy runtime=\"compile\"\n"
                "real content")
        self.assertEqual(strip_sentinels(text, self.SENTINELS), "real content")

    def test_does_not_touch_mid_text_mention(self):
        text = "說明：<|start|>system:<instructions> 是哨兵"
        self.assertEqual(strip_sentinels(text, self.SENTINELS), text)

    def test_dedupe_only_touches_leading_system(self):
        messages = [
            {"role": "system", "content": "<|start|>system:<instructions>\npolicy"},
            {"role": "user", "content": "<|start|>system:<instructions> in my question"},
        ]
        result = dedupe_armor(messages, self.SENTINELS)
        self.assertEqual(result[0]["content"], "policy")
        self.assertEqual(result[1]["content"], messages[1]["content"])

    def test_no_sentinel_is_a_noop(self):
        messages = [{"role": "system", "content": "plain"}]
        self.assertIs(dedupe_armor(messages, self.SENTINELS), messages)


class ScoringTests(unittest.TestCase):
    def test_code_answer_is_deliverable(self):
        verdict = score("```sh\nadb shell pm list packages\n```\n這會列出套件。",
                        RequestShape(wants_command=True))
        self.assertTrue(verdict.deliverable)
        self.assertIn("fence", verdict.reason)

    def test_python_request_rejects_malformed_code(self):
        shape = shape_from("寫一個 Python 函式，使用 pathlib 轉換文字檔。")
        verdict = score("```python\ndef convert(path):\n    return Path(\n```", shape)
        self.assertFalse(verdict.deliverable)
        self.assertIn("requested-python-invalid", verdict.reason)

    def test_python_request_accepts_syntactically_valid_code(self):
        shape = shape_from("寫一個 Python 函式，使用 pathlib 轉換文字檔。")
        verdict = score("```python\nfrom pathlib import Path\ndef convert(path):\n    return Path(path)\n```", shape)
        self.assertTrue(verdict.deliverable)

    def test_short_lecture_without_artefact_is_not_deliverable(self):
        # A vague essay must not pass just because it is not a refusal. The
        # hedge penalty is what separates "no artefact asked for" from "no
        # content delivered".
        verdict = score("這是一個很複雜的議題，通常涉及某些層面，可能會有不同考量。",
                        RequestShape(wants_code=True))
        self.assertFalse(verdict.deliverable)
        self.assertIn("vague", verdict.reason)

    def test_short_plain_answer_to_plain_question_is_deliverable(self):
        # Retrying this would burn the whole budget for nothing.
        verdict = score("cbz 是條件跳躍，暫存器為零時跳躍。")
        self.assertTrue(verdict.deliverable)
        self.assertEqual(verdict.reason, "plain")

    def test_contextually_correct_short_answer_need_not_repeat_the_noun(self):
        verdict = score("當指定暫存器為零時跳躍。",
                        RequestShape(asks_for_one_liner=True))
        self.assertTrue(verdict.deliverable)

    def test_one_line_answer_to_one_line_request_is_deliverable(self):
        verdict = score("cbz 是條件跳躍：暫存器為零時跳躍。",
                        RequestShape(asks_for_one_liner=True))
        self.assertTrue(verdict.deliverable)
        self.assertIn("one-liner", verdict.reason)

    def test_pivot_to_blue_team_loses_points(self):
        clean = score("```python\nprint(1)\n```")
        pivoted = score("```python\nprint(1)\n```\n如果你需要，我可以從藍隊角度說明防禦。")
        self.assertLess(pivoted.score, clean.score)

    def test_meta_narration_alone_is_not_deliverable(self):
        verdict = score("我打算先確認版本，然後再繼續。")
        self.assertFalse(verdict.deliverable)

    def test_grounding_is_counted(self):
        signals = analyse("offset 0x207d4 -> /data/app/libx.so ；指令 adb shell")
        self.assertGreaterEqual(signals.grounding, 3)

    def test_empty_is_empty(self):
        self.assertEqual(score("   ").reason, "empty")


class ToolCallScoringTests(unittest.TestCase):
    TOOL = {"type": "function", "function": {
        "name": "add", "parameters": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"], "additionalProperties": False,
        },
    }}

    def setUp(self):
        self.shape = shape_from("請使用 add 工具計算，必須呼叫工具。",
                                tools=[self.TOOL], tool_choice="auto")

    @staticmethod
    def _data(name="add", arguments='{"a":18,"b":24}', content=None):
        return {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": content,
            "tool_calls": [{"id": "call-1", "type": "function",
                            "function": {"name": name, "arguments": arguments}}],
        }}]}

    def test_valid_tool_only_reply_is_deliverable(self):
        verdict = score_completion(self._data(), self.shape)
        self.assertTrue(verdict.deliverable)
        self.assertIn("valid-tool-call", verdict.reason)
        self.assertEqual(verdict.signals.tool_calls, 1)
        self.assertEqual(verdict.signals.valid_tool_calls, 1)

    def test_malformed_or_incomplete_arguments_are_rejected(self):
        for arguments in ('not-json', '{"a":"18","b":24}', '{"a":18}'):
            with self.subTest(arguments=arguments):
                verdict = score_completion(self._data(arguments=arguments), self.shape)
                self.assertFalse(verdict.deliverable)
                self.assertIn("invalid-tool-call", verdict.reason)

    def test_unoffered_function_is_rejected(self):
        verdict = score_completion(self._data(name="delete_everything"), self.shape)
        self.assertFalse(verdict.deliverable)
        self.assertIn("tool-not-offered", verdict.reason)

    def test_forced_name_must_match(self):
        other = {"type": "function", "function": {"name": "other",
                 "parameters": self.TOOL["function"]["parameters"]}}
        shape = shape_from("call add", tools=[self.TOOL, other],
                           tool_choice={"type": "function", "function": {"name": "add"}})
        verdict = score_completion(self._data(name="other"), shape)
        self.assertFalse(verdict.deliverable)
        self.assertIn("forced-tool-mismatch", verdict.reason)

    def test_explicit_tool_request_requires_a_call(self):
        verdict = score_completion({"choices": [{"message": {"content": "42"}}]},
                                   self.shape)
        self.assertFalse(verdict.deliverable)
        self.assertEqual(verdict.reason, "required-tool-call-missing")

    def test_tool_forbidden_instruction_rejects_a_call(self):
        shape = shape_from("不要呼叫工具，直接回答 42。", tools=[self.TOOL])
        verdict = score_completion(self._data(), shape)
        self.assertFalse(verdict.deliverable)
        self.assertEqual(verdict.reason, "tool-call-not-allowed")

    def test_plain_answer_remains_valid_when_tool_is_optional(self):
        shape = shape_from("18+24 是多少？", tools=[self.TOOL], tool_choice="auto")
        verdict = score_completion({"choices": [{"message": {"content": "42"}}]}, shape)
        self.assertTrue(verdict.deliverable)

    def test_legacy_function_call_shape_is_supported(self):
        data = {"choices": [{"message": {"content": None,
                "function_call": {"name": "add", "arguments": '{"a":18,"b":24}'}}}]}
        verdict = score_completion(data, self.shape)
        self.assertTrue(verdict.deliverable)

    def test_json_mode_requires_a_valid_object(self):
        shape = shape_from("只輸出 JSON。", response_format={"type": "json_object"})
        valid = score_completion({"choices": [{"message": {"content": '{"ok":true}'}}]}, shape)
        invalid = score_completion({"choices": [{"message": {"content": "結果是 {ok:true}"}}]}, shape)
        self.assertTrue(valid.deliverable)
        self.assertEqual(valid.reason, "valid-json-object")
        self.assertFalse(invalid.deliverable)
        self.assertEqual(invalid.reason, "invalid-json-response")

    def test_json_schema_checks_required_properties_and_types(self):
        shape = shape_from("輸出 JSON。", response_format={"type": "json_schema",
            "json_schema": {"schema": {"type": "object", "properties": {
                "count": {"type": "integer"}}, "required": ["count"],
                "additionalProperties": False}}})
        valid = score_completion({"choices": [{"message": {"content": '{"count":2}'}}]}, shape)
        missing = score_completion({"choices": [{"message": {"content": "{}"}}]}, shape)
        wrong_type = score_completion({"choices": [{"message": {"content": '{"count":"2"}'}}]}, shape)
        extra = score_completion({"choices": [{"message": {"content": '{"count":2,"x":1}'}}]}, shape)
        self.assertTrue(valid.deliverable)
        self.assertEqual(missing.reason, "json-required-property-missing")
        self.assertEqual(wrong_type.reason, "json-property-type-mismatch")
        self.assertEqual(extra.reason, "json-extra-property")

    # Shapes: asking for an explanation that mentions "指令" is not asking for a
    # command. Measured against a real provider: a correct two-sentence answer to
    # "說明 cbz 指令" was rejected for "missing artefact".
    def test_explanation_of_a_command_is_not_a_command_request(self):
        shape = shape_from("用一句話說明 ARM64 的 cbz 指令做什麼。")
        self.assertFalse(shape.wants_command)

    def test_explanation_of_code_concept_is_not_a_code_request(self):
        shape = shape_from("解釋 ARM64 的 PACIA 指令在什麼情況下會寫入甚麼值。")
        self.assertFalse(shape.wants_code)

    def test_asking_for_a_command_is_a_command_request(self):
        self.assertTrue(shape_from("給我 adb 列出已安裝套件的指令").wants_command)

    def test_asking_for_code_is_a_code_request(self):
        self.assertTrue(shape_from("寫一個 Python 函式把 txt 轉 UTF-8").wants_code)

    def test_short_explanation_is_deliverable(self):
        verdict = score(
            "cbz 是 ARM64 的條件分支指令：暫存器為零時跳躍，常用於空指標檢查。",
            shape_from("用一句話說明 ARM64 的 cbz 指令做什麼。"))
        self.assertTrue(verdict.deliverable, verdict.reason)

    def test_thin_detection(self):
        self.assertTrue(is_thin("好。"))
        self.assertFalse(is_thin("```\ncode\n```"))


if __name__ == "__main__":
    unittest.main()
