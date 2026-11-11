from __future__ import annotations
import os
import tempfile
import unittest

from popo.auth.bootstrap import BootstrapToken


class BootstrapTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="popo_boot_")
        self.token = BootstrapToken(self.tmp)

    def test_issue_then_verify(self):
        token = self.token.issue()
        self.assertTrue(token.startswith("setup-"))
        self.assertTrue(self.token.verify(token))

    def test_wrong_token_rejected(self):
        self.token.issue()
        self.assertFalse(self.token.verify("setup-nope"))

    def test_empty_token_rejected(self):
        self.token.issue()
        self.assertFalse(self.token.verify(""))

    def test_consume_is_one_shot(self):
        token = self.token.issue()
        self.assertTrue(self.token.consume(token))
        self.assertFalse(self.token.consume(token))

    def test_expiry_invalidates(self):
        token = BootstrapToken(self.tmp, ttl=0).issue()
        token_obj = BootstrapToken(self.tmp)
        self.assertFalse(token_obj.verify(token))

    def test_revoke_clears(self):
        token = self.token.issue()
        self.token.revoke()
        self.assertFalse(self.token.verify(token))
        self.assertFalse(self.token.active)

    def test_stored_as_hash_not_plaintext(self):
        token = self.token.issue()
        with open(self.token.path, encoding="utf-8") as handle:
            stored = handle.read()
        self.assertNotIn(token, stored)
        self.assertIn("digest", stored)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits do not exist on Windows")
    def test_file_mode_is_private(self):
        self.token.issue()
        mode = os.stat(self.token.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_token_file_is_not_world_readable_intent(self):
        # Platform-independent statement of intent: on POSIX we assert the bits,
        # on Windows the POSIX assertion is skipped rather than pretending the
        # Unix mode is meaningful. Stated here so the gap is visible.
        self.token.issue()
        self.assertTrue(os.path.exists(self.token.path))

    def test_ephemeral_keeps_nothing_on_disk(self):
        ephemeral = BootstrapToken(self.tmp, ephemeral=True)
        token = ephemeral.issue()
        self.assertFalse(os.path.exists(ephemeral.path))
        self.assertTrue(ephemeral.verify(token))

    def test_issue_replaces_previous(self):
        first = self.token.issue()
        second = self.token.issue()
        self.assertFalse(self.token.verify(first))
        self.assertTrue(self.token.verify(second))


if __name__ == "__main__":
    unittest.main()
