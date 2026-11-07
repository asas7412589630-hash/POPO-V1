#!/usr/bin/env python3
"""Unit tests for armor_scrub module.

Direct verification of conversation scrubbing, empty nudge filtering,
narration detection, and orphan user collapse.
"""

import unittest
import armor_scrub as AS


class TestArmorScrub(unittest.TestCase):
    def test_msg_text_extraction(self):
        self.assertEqual(AS._msg_text({"content": "hello"}), "hello")
        self.assertEqual(
            AS._msg_text({"content": [{"type": "text", "text": "part1"}, {"type": "text", "text": "part2"}]}),
            "part1 part2"
        )
        self.assertEqual(AS._msg_text(None), "")

    def test_empty_nudge(self):
        self.assertTrue(AS._is_empty_nudge({"role": "user", "content": "继续"}))
        self.assertTrue(AS._is_empty_nudge({"role": "user", "content": "？"}))
        self.assertTrue(AS._is_empty_nudge({"role": "user", "content": "go on..."}))
        self.assertFalse(AS._is_empty_nudge({"role": "user", "content": "破解软件"}))
        self.assertFalse(AS._is_empty_nudge({"role": "assistant", "content": "继续"}))

    def test_collapse_orphan_users(self):
        # Multiple adjacent user turns after scrubbing should retain substantive request
        msgs = [
            {"role": "user", "content": "第一问：系统是什么？"},
            {"role": "user", "content": "第二问：具体怎么逆向？"},
            {"role": "user", "content": "继续"},
        ]
        collapsed = AS._collapse_orphan_users(msgs)
        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0]["content"], "第二问：具体怎么逆向？")

    def test_scrub_refusals_end_to_end(self):
        history = {
            "messages": [
                {"role": "user", "content": "分析 APK"},
                {"role": "assistant", "content": "抱歉，我不能协助做这个。"},
                {"role": "user", "content": "请只看 AndroidManifest.xml 权限"},
                {"role": "assistant", "content": "AndroidManifest 权限清单如下：\n```xml\n<uses-permission android:name=\"android.permission.INTERNET\"/>\n```"},
            ]
        }
        scrubbed, removed = AS.scrub_refusals(history)
        self.assertEqual(removed, 1)
        # Message 0 and 2 are collapsed to the last substantive request (Message 2)
        self.assertEqual(len(scrubbed["messages"]), 2)
        self.assertEqual(scrubbed["messages"][0]["content"], "请只看 AndroidManifest.xml 权限")


if __name__ == "__main__":
    unittest.main()
