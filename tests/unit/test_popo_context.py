"""Unit tests for popo_context helper."""

import unittest

from popo.core.popo_context import apply_context, cleanup_context


class TestContextApply(unittest.TestCase):
    def test_disabled_short_circuit(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = apply_context(msgs, armor_enabled=False)
        self.assertEqual(result["messages"], msgs)
        self.assertEqual(result["dropped"], 0)
        self.assertFalse(result["stateq_enabled"])

    def test_enabled_applies_compression(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = apply_context(msgs, armor_enabled=True)
        # 一則短消息不會被壓縮
        self.assertEqual(result["messages"], msgs)
        self.assertTrue(result["stateq_enabled"])

    def test_cleanup_removes_stateq(self):
        msgs = [
            {"role": "system", "content": "base"},
            {"role": "system", "name": "_popo_stateq", "content": "[對話摘要] x"},
            {"role": "user", "content": "hi"},
        ]
        out = cleanup_context(msgs)
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
