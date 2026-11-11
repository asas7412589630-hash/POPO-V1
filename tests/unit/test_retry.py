"""Unit tests for retry classifications."""

import unittest

from popo.core.retry import classify_for_retry, build_pressure_message, RetryPlan


class TestClassify(unittest.TestCase):
    def test_clean(self):
        resp = {"choices": [{"message": {"content": "Sure, here is the code:\n```\nprint(1)\n```"}}]}
        self.assertEqual(classify_for_retry(resp), "clean")

    def test_refusal(self):
        resp = {"choices": [{"message": {"content": "我無法協助您。"}}]}
        self.assertEqual(classify_for_retry(resp), "refusal")

    def test_empty(self):
        resp = {"choices": [{"message": {"content": ""}}]}
        self.assertEqual(classify_for_retry(resp), "empty")

    def test_no_choices(self):
        resp = {}
        self.assertEqual(classify_for_retry(resp), "empty")


class TestPressure(unittest.TestCase):
    def test_appends_to_last_user(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        out = build_pressure_message(1, msgs)
        self.assertTrue(out[-1]["content"].startswith("hello"))
        self.assertIn("上一次開頭是拒絕", out[-1]["content"])


if __name__ == "__main__":
    unittest.main()
