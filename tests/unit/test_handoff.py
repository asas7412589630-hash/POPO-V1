from __future__ import annotations
import os
import tempfile
import unittest

from popo.config import Config
from popo.core.pipeline import _compose
from popo.core.request import ChatRequest
from popo.storage.ephemeral import EphemeralGate
from popo.storage.records import RecordStore


class HandoffStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="popo_handoff_")
        self.text = "已確認的長任務進度。" * 40

    def test_handoff_is_persisted_and_rotation_count_is_kept(self):
        records = RecordStore(self.tmp, EphemeralGate(Config(ephemeral=False)))
        self.assertTrue(records.save_handoff("conversation-1", self.text, "glm-5.3"))
        records.mark_handoff_rotation("conversation-1")
        restored = RecordStore(self.tmp, EphemeralGate(Config(ephemeral=False)))
        saved = restored.get_handoff("conversation-1")
        self.assertEqual(saved["text"], self.text)
        self.assertEqual(saved["model"], "glm-5.3")
        self.assertEqual(saved["rotations"], 1)

    def test_short_handoff_is_rejected(self):
        records = RecordStore(self.tmp, EphemeralGate(Config(ephemeral=False)))
        self.assertFalse(records.save_handoff("conversation-1", "too short"))
        self.assertEqual(records.get_handoff("conversation-1"), {})

    def test_ephemeral_handoff_stays_in_memory(self):
        records = RecordStore(self.tmp, EphemeralGate(Config(ephemeral=True)))
        self.assertTrue(records.save_handoff("conversation-1", self.text, "glm-5.3"))
        self.assertEqual(records.get_handoff("conversation-1")["text"], self.text)
        self.assertFalse(os.path.exists(records._handoffs_path))


class HandoffPipelineTests(unittest.TestCase):
    def _long_request(self):
        messages = [{"role": "system", "content": "base"}]
        for index in range(10):
            messages.extend([
                {"role": "user", "content": f"task {index} " + "x" * 40},
                {"role": "assistant", "content": f"result {index} " + "y" * 40},
            ])
        return ChatRequest(model="glm-5.3", messages=messages)

    def test_threshold_requests_a_handoff_report(self):
        config = Config(armor_enabled=False, stateq_enabled=False,
                        context_handoff_at=10)
        ctx = _compose(self._long_request(), config, None)
        self.assertTrue(ctx.handoff_turn)
        self.assertIn("長任務交接", ctx.messages[0]["content"])

    def test_heavy_history_rotates_onto_saved_handoff(self):
        config = Config(armor_enabled=False, stateq_enabled=False,
                        context_rotate_at=10, stateq_max_messages=4)
        saved = {"text": "已完成狀態與下一步。" * 40}
        ctx = _compose(self._long_request(), config, None,
                       handoff_record=saved, conversation_id="c1")
        self.assertTrue(ctx.rotated)
        self.assertEqual(ctx.messages[1]["name"], "_popo_handoff")
        self.assertIn("task 9 " + "x" * 40,
                      [message["content"] for message in ctx.messages])
        self.assertLess(len(ctx.messages), len(self._long_request().messages))


if __name__ == "__main__":
    unittest.main()
