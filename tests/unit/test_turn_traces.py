import os
import shutil
import tempfile
import time
import unittest

from popo.config import Config
from popo.storage.ephemeral import EphemeralGate
from popo.storage.records import RecordStore


class TurnTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="popo_trace_")
        self.records = RecordStore(self.tmp, EphemeralGate(Config(ephemeral=False)))
        self.records.append_turn({
            "client": "key-a", "conv": "conv-1", "model": "glm-5.3",
            "user": "show a command", "verdict": "retry_ok", "final": "done",
            "attempts": [{"n": 1, "kind": "refusal", "text": "no"},
                         {"n": 2, "kind": "ok", "text": "done"}],
            "final_chars": 4, "ts": time.time() - 30,
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hierarchical_indexes_and_lazy_detail(self):
        clients = self.records.list_trace_clients(7)
        self.assertEqual(clients[0]["client"], "key-a")
        self.assertEqual(clients[0]["convs"], ["conv-1"])
        convs = self.records.list_trace_conversations("key-a", 7)
        self.assertEqual(convs[0]["turns"], 1)
        turns = self.records.list_turns("key-a", "conv-1", 7)
        self.assertEqual(turns[0]["attempt_count"], 2)
        self.assertNotIn("final", turns[0])
        full = self.records.get_turn(turns[0]["id"])
        self.assertEqual(full["attempts"][0]["text"], "no")
        self.assertEqual(full["final"], "done")

    def test_client_and_conversation_filters(self):
        self.records.append_turn({"client": "key-b", "conv": "conv-2", "user": "other"})
        self.assertEqual(len(self.records.list_trace_conversations("key-a", 7)), 1)
        self.assertEqual(len(self.records.list_turns("key-b", "conv-1", 7)), 0)

    def test_conversation_label_uses_earliest_user_turn(self):
        self.records.append_turn({"client": "key-a", "conv": "conv-1",
                                  "model": "glm-5.3", "user": "later follow-up",
                                  "ts": time.time()})
        conv = self.records.list_trace_conversations("key-a", 7)[0]
        self.assertEqual(conv["first_user"], "show a command")

    def test_ephemeral_mode_does_not_write_turn_data(self):
        ephemeral_dir = os.path.join(self.tmp, "ephemeral")
        ephemeral = RecordStore(ephemeral_dir, EphemeralGate(Config(ephemeral=True)))
        ephemeral.append_turn({"client": "secret", "conv": "private"})
        self.assertEqual(ephemeral.list_trace_clients(), [])
        self.assertFalse(os.path.isdir(os.path.join(ephemeral_dir, "runtime", "trace")))


if __name__ == "__main__":
    unittest.main()
