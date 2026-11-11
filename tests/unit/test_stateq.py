"""Unit tests for stateq context folding/compression."""

import unittest

from popo.core.stateq import (
    COMPRESS_MARKER,
    HISTORY_MARKER,
    StateCfg,
    compress,
    fold,
    restore,
    stats,
)


class TestFold(unittest.TestCase):
    def test_short_history_passes_through(self):
        msgs = [
            {"role": "system", "content": "base"},
            {"role": "user", "content": "hi"},
        ]
        fr = fold(msgs, StateCfg(enabled=True, max_messages=8))
        self.assertEqual(fr.messages, msgs)
        self.assertEqual(fr.dropped, 0)

    def test_fold_keeps_first_system_and_last_pair(self):
        """超過 max_messages 時，首 system 與最後一對 user/assistant 保留。"""
        msgs: list[dict] = [
            {"role": "system", "content": "base"},
        ]
        for i in range(8):
            msgs.append({"role": "user", "content": f"q{i}"})
            msgs.append({"role": "assistant", "content": f"a{i}"})
        fr = fold(msgs, StateCfg(enabled=True, max_messages=6))
        self.assertEqual(fr.messages[0]["role"], "system")
        self.assertEqual(fr.messages[0]["content"], "base")
        self.assertEqual(fr.messages[-2]["content"], "q7")
        self.assertEqual(fr.messages[-1]["content"], "a7")

    def test_disabled_returns_copy(self):
        msgs = [{"role": "user", "content": "x"}] * 20
        fr = fold(msgs, StateCfg(enabled=False))
        self.assertEqual(len(fr.messages), 20)


class TestCompress(unittest.TestCase):
    def test_char_threshold_triggers_drop(self):
        # 製造一則超長 system 摘要，讓總字數超過 threshold
        long_text = "x" * 2000
        stateq_summary = {"role": "system", "name": "_popo_stateq", "content": f"{COMPRESS_MARKER} {long_text}"}
        msgs = [
            {"role": "system", "content": "base"},
            stateq_summary,
            {"role": "user", "content": "final"},
        ]
        cfg = StateCfg(enabled=True, max_messages=20, compress_threshold=100)
        out = compress(msgs, cfg)
        # 最後一則 user 必須保留
        self.assertEqual(out.messages[-1]["content"], "final")
        # 壓縮後最多只保留第一則 system 與最後 user；長摘要應被移除
        self.assertLess(len([m for m in out.messages if m.get("name") == "_popo_stateq"]), 2)
        # 原始第一則 system 內容保留
        self.assertEqual(out.messages[0]["content"], "base")

    def test_under_threshold_unchanged(self):
        msgs = [
            {"role": "system", "content": "base"},
            {"role": "user", "content": "hi"},
        ]
        out = compress(msgs, StateCfg(enabled=True, compress_threshold=500))
        self.assertEqual(len(out.messages), 2)

    def test_disabled_short_circuit(self):
        msgs = [{"role": "user", "content": "x" * 3000}]
        out = compress(msgs, StateCfg(enabled=False))
        self.assertEqual(out.messages, msgs)


class TestRestore(unittest.TestCase):
    def test_removes_stateq_markers(self):
        msgs = [
            {"role": "system", "content": "base"},
            {"role": "system", "name": "_popo_stateq", "content": f"{COMPRESS_MARKER} x"},
            {"role": "user", "content": "hi"},
        ]
        out = restore(msgs)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["content"], "base")
        self.assertEqual(out[1]["content"], "hi")


class TestStats(unittest.TestCase):
    def test_counts_roles(self):
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},
        ]
        s = stats(msgs)
        self.assertEqual(s["messages"], 3)
        self.assertEqual(s["role_system"], 1)
        self.assertEqual(s["role_user"], 1)
        self.assertEqual(s["role_assistant"], 1)
        self.assertGreater(s["estimated_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
