"""Unit tests for key management."""

import json
import os
import tempfile
import unittest

from popo.auth.keys import KeyStore
from popo.storage.ephemeral import EphemeralGate


class TestKeyStore(unittest.TestCase):
    def test_create_verify(self):
        store = KeyStore()
        token, rec = store.create(label="test")
        self.assertTrue(token.startswith("triad-"))
        self.assertEqual(store.verify(token).key_id, rec.key_id)
        self.assertFalse(rec.allow_public)

    def test_public_scope_is_recorded(self):
        store = KeyStore()
        token, rec = store.create(label="public", allow_public=True)
        self.assertTrue(store.verify(token).allow_public)
        self.assertTrue(rec.allow_public)

    def test_legacy_keys_keep_previous_public_access(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "keys.json"), "w", encoding="utf-8") as handle:
                json.dump({"keys": [{"key_id": "legacy", "hash": "hash"}]}, handle)
            store = KeyStore(directory, EphemeralGate())
            self.assertTrue(store.list_all()[0].allow_public)

    def test_wrong_token_fails(self):
        store = KeyStore()
        store.create()
        self.assertIsNone(store.verify("triad-wrong"))

    def test_disable(self):
        store = KeyStore()
        token, rec = store.create()
        self.assertTrue(store.disable(rec.key_id))
        self.assertIsNone(store.verify(token))

    def test_disable_unknown_returns_false(self):
        store = KeyStore()
        self.assertFalse(store.disable("nope"))

    def test_delete(self):
        store = KeyStore()
        token, rec = store.create(label="del")
        self.assertTrue(store.delete(rec.key_id))
        self.assertIsNone(store.verify(token))


if __name__ == "__main__":
    unittest.main()
