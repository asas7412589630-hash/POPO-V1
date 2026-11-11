import os
import shutil
import tempfile
import unittest

from popo.config import Config
from popo.storage.ephemeral import EphemeralGate
from popo.storage.state_ledger import StateLedgerStore


class StateLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="popo_ledger_")
        self.gate = EphemeralGate(Config(ephemeral=False))
        self.ledger = StateLedgerStore(self.tmp, self.gate)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_carries_identifiers_across_conversations_without_copying_prompts(self):
        self.ledger.update("client-key-id", [
            {"role": "user", "content": "分析 /workspace/app/base.apk，已找到 0x5b2e8"},
            {"role": "tool", "content": "Java_com_demo_Main::verify at 0x5b310"},
        ])
        restored = StateLedgerStore(self.tmp, self.gate)
        block = restored.prompt_block("client-key-id")
        self.assertIn("base.apk", block)
        self.assertIn("0x5b2e8", block)
        self.assertIn("Java_com_demo_Main::verify", block)
        self.assertNotIn("分析", block)
        self.assertEqual(restored.get("another-client"), {})

    def test_ephemeral_ledger_is_memory_only(self):
        gate = EphemeralGate(Config(ephemeral=True))
        ledger = StateLedgerStore(self.tmp, gate)
        ledger.update("client", [{"role": "user", "content": "file.so 0x1234"}])
        self.assertTrue(ledger.get("client")["targets"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "state", "ledgers.json")))


if __name__ == "__main__":
    unittest.main()
