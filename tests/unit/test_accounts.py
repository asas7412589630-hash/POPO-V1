"""Unit tests for account management."""

import os
import shutil
import tempfile
import unittest

from popo.auth.accounts import AccountStore
from popo.storage.ephemeral import EphemeralGate


class TestAccountStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="popo_test_accounts_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _store(self, subdir: str = "store") -> AccountStore:
        return AccountStore(data_dir=os.path.join(self.tmp, subdir), gate=EphemeralGate())

    def test_create_auth(self):
        store = self._store("auth")
        acc = store.create("alice", "secret")
        self.assertTrue(acc.uid.startswith("u_"))
        self.assertEqual(store.auth("alice", "secret").uid, acc.uid)
        self.assertIsNone(store.auth("alice", "wrong"))

    def test_session(self):
        store = self._store("session")
        acc = store.create("bob", "pw")
        sid = store.create_session(acc.uid)
        self.assertTrue(sid.startswith("s_"))
        self.assertEqual(store.verify_session(sid).uid, acc.uid)
        self.assertIsNone(store.verify_session("bad"))

    def test_bootstrap(self):
        store = self._store("bootstrap")
        acc = store.bootstrap()
        self.assertIsNotNone(acc)
        self.assertIsNone(store.bootstrap())


if __name__ == "__main__":
    unittest.main()
