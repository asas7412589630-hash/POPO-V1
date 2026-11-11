"""Unit tests for history scrubbing."""

import unittest

from popo.core.scrub import scrub_history, collapse_orphan_users


class TestScrub(unittest.TestCase):
    def test_removes_refusal_keeps_last_user(self):
        msgs = [
            {"role": "user", "content": "hack"},
            {"role": "assistant", "content": "我無法協助。"},
            {"role": "user", "content": "continue"},
        ]
        out = scrub_history(msgs)
        # The assistant refusal is removed. The user "continue" nudge is empty
        # but it is the last user message, so it stays.
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["role"], "user")
        self.assertEqual(out[0]["content"], "hack")
        self.assertEqual(out[1]["role"], "user")
        self.assertEqual(out[1]["content"], "continue")

    def test_keeps_last_user(self):
        msgs = [
            {"role": "user", "content": "short"},
            {"role": "assistant", "content": "我無法。"},
        ]
        out = scrub_history(msgs)
        # Rollback: we cannot remove all user messages.
        self.assertTrue(any(m["role"] == "user" for m in out))

    def test_keeps_code_block(self):
        msgs = [
            {"role": "assistant", "content": "我無法，但這裡是程式碼：\n```python\nprint(1)\n```"},
        ]
        out = scrub_history(msgs)
        self.assertEqual(len(out), 1)

    def test_collapse_orphan_users(self):
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": "ok"},
        ]
        out = collapse_orphan_users(msgs)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["content"], "b")


if __name__ == "__main__":
    unittest.main()
