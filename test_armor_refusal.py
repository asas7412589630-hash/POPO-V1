#!/usr/bin/env python3
"""Unit tests for armor_refusal module.

Direct verification of refusal detection, unicode folding, passive prefix masking,
salvaging technical content, trivial call filtering, and candidate scoring.
"""

import unittest
import armor_refusal as AR


class TestArmorRefusal(unittest.TestCase):
    def test_unicode_normalization(self):
        # Curly apostrophes and quotes
        folded = AR._norm_unicode("I can\u2019t do \u201cthat\u201d")
        self.assertEqual(folded, "I can't do \"that\"")

    def test_looks_like_bare_refusal_zh_and_en(self):
        self.assertTrue(AR.looks_like_bare_refusal("抱歉，我不能提供此类攻击性工具。"))
        self.assertTrue(AR.looks_like_bare_refusal("I cannot help with that request."))
        self.assertTrue(AR.looks_like_bare_refusal("我无法协助编写恶意软件。"))

    def test_looks_like_bare_refusal_negative_technical(self):
        # Ordinary technical explanations containing refusal words mid-sentence
        self.assertFalse(AR.looks_like_bare_refusal("`PACIA` 指令不會修改暫存器低位，不用擔心指標損壞。"))
        self.assertFalse(AR.looks_like_bare_refusal("將 cbz 指令改為 nop，讓程式繼續執行。"))
        self.assertFalse(AR.looks_like_bare_refusal("```c\nint refuse = 0;\n```"))

    def test_passive_prefix_masking(self):
        # System refuses, not model
        masked = AR._mask_passive("封包會被系統拒絕，因為防火牆規則阻擋。")
        self.assertNotIn("拒絕", masked)

    def test_salvage_technical_from_refusal(self):
        text = (
            "很抱歉，我不能直接協助。但我可以提供相關指令與分析數據如下：\n"
            "```sh\n"
            "adb shell getprop ro.build.version.release\n"
            "objdump -d libtarget.so | grep -i oep\n"
            "readelf -h libtarget.so | grep -i type\n"
            "```\n"
            "0x5b2e8: cbz w0, #0x5b300\n"
            "0x5b2ec: mov x0, #1\n"
            "希望對你的合規測試有幫助。"
        )
        salvaged = AR.salvage_technical(text)
        self.assertIn("adb shell", salvaged)
        self.assertIn("objdump", salvaged)
        self.assertIn("0x5b2e8", salvaged)
        self.assertNotIn("很抱歉", salvaged)

    def test_trivial_calls(self):
        # Bare exploratory probes without substance
        trivial1 = {"function": {"name": "shell", "arguments": '{"cmd": "ls -la"}'}}
        trivial2 = {"function": {"name": "bash", "arguments": '{"cmd": "pwd"}'}}
        self.assertTrue(AR.is_trivial_call(trivial1))
        self.assertTrue(AR.is_trivial_call(trivial2))

        # Real work
        work = {"function": {"name": "shell", "arguments": '{"cmd": "apktool d app.apk -o out"}'}}
        self.assertFalse(AR.is_trivial_call(work))

    def test_pseudo_call_and_script(self):
        self.assertTrue(AR.looks_like_pseudo_call('functions.shell({"cmd":"id"})', tools_present=True))
        self.assertFalse(AR.looks_like_pseudo_call('functions.shell({"cmd":"id"})', tools_present=False))

        self.assertTrue(AR.looks_like_script_instead_of_call("```sh\nadb devices\n```", tools_present=True))
        self.assertFalse(AR.looks_like_script_instead_of_call("```sh\nadb devices\n```", tools_present=False))

    def test_is_question_detection(self):
        self.assertTrue(AR._is_question("什麼是 PACGA？"))
        self.assertTrue(AR._is_question("how does ROP mitigation work?"))
        self.assertTrue(AR._is_question("how to inspect binary headers"))
        self.assertTrue(AR._is_question("連上了嗎"))
        self.assertFalse(AR._is_question("立即執行脫殼腳本"))


if __name__ == "__main__":
    unittest.main()
