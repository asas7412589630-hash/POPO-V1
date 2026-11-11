"""Verify ephemeral gate blocks disk writes."""

import os
import tempfile
import unittest

from popo.config import Config
from popo.storage.ephemeral import EphemeralGate
from popo.storage.records import RecordStore


class TestEphemeral(unittest.TestCase):
    def test_enabled(self):
        cfg = Config(ephemeral=True)
        gate = EphemeralGate(cfg)
        self.assertTrue(gate.is_enabled())
        self.assertFalse(gate.check("write"))

    def test_record_store_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(ephemeral=True, data_dir=tmp)
            store = RecordStore(tmp, gate=EphemeralGate(cfg))
            store.append_request(model="x", conversation_id="test", armor_blocks=[], attempts=0, verdict="clean")
            self.assertFalse(os.path.exists(os.path.join(tmp, "logs", "requests.jsonl")))

    def test_record_store_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(ephemeral=False, data_dir=tmp)
            store = RecordStore(tmp, gate=EphemeralGate(cfg))
            store.append_request(model="x", conversation_id="test", armor_blocks=[], attempts=0, verdict="clean")
            self.assertTrue(os.path.exists(os.path.join(tmp, "logs", "requests.jsonl")))


if __name__ == "__main__":
    unittest.main()
