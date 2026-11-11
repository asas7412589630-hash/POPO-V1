"""Unit tests for the identity gate."""

import hashlib
import unittest

from popo.auth.accounts import AccountStore
from popo.auth.gate import Gate
from popo.auth.keys import KeyStore
from popo.config import Config
from popo.errors import ProxyError
from popo.server.routes import authorize, match_route
from popo.storage.ephemeral import EphemeralGate


class TestGate(unittest.TestCase):
    def setUp(self):
        self.config = Config(require_client_key=True, loopback_trusted=True)
        self.keystore = KeyStore()
        self.accounts = AccountStore(gate=EphemeralGate())
        self.admin_hashes = set()

    def _gate(self, headers: dict, addr: tuple[str, int]) -> Gate:
        return Gate(
            self.config, headers, addr,
            keystore=self.keystore, accounts=self.accounts, admin_hashes=self.admin_hashes,
        )

    def test_loopback_no_key_is_loopback(self):
        identity = self._gate({}, ("127.0.0.1", 12345)).resolve()
        self.assertEqual(identity.level, "loopback")
        self.assertEqual(identity.via, "loopback")

    def test_client_key_trumps_loopback(self):
        token, rec = self.keystore.create()
        identity = self._gate({"Authorization": f"Bearer {token}"}, ("127.0.0.1", 12345)).resolve()
        self.assertEqual(identity.level, "client-key")
        self.assertEqual(identity.client_key_id, rec.key_id)

    def test_lowercase_authorization_survives_a_forwarded_tunnel(self):
        token, rec = self.keystore.create()
        identity = self._gate({"authorization": f"Bearer {token}",
                               "x-forwarded-for": "public-tunnel"},
                              ("127.0.0.1", 12345)).resolve()
        self.assertEqual(identity.level, "client-key")
        self.assertEqual(identity.client_key_id, rec.key_id)

    def test_internal_key_is_rejected_at_public_tunnel(self):
        token, _rec = self.keystore.create()
        gate = self._gate({"Authorization": f"Bearer {token}",
                           "X-Forwarded-For": "public-tunnel"},
                          ("127.0.0.1", 12345))
        identity = gate.resolve()
        self.assertTrue(identity.external_request)
        with self.assertRaises(ProxyError):
            authorize(match_route("POST", "/v1/chat/completions"), identity,
                      self.config)

    def test_public_key_is_accepted_at_public_tunnel(self):
        token, _rec = self.keystore.create(allow_public=True)
        gate = self._gate({"Authorization": f"Bearer {token}",
                           "X-Forwarded-For": "public-tunnel"},
                          ("127.0.0.1", 12345))
        identity = gate.resolve()
        self.assertTrue(identity.external_request)
        authorize(match_route("POST", "/v1/chat/completions"), identity,
                  self.config)

    def test_public_key_is_rejected_on_operator_routes_through_tunnel(self):
        token, _rec = self.keystore.create(allow_public=True)
        gate = self._gate({"Authorization": f"Bearer {token}",
                           "X-Forwarded-For": "public-tunnel"},
                          ("127.0.0.1", 12345))
        with self.assertRaises(ProxyError):
            authorize(match_route("GET", "/admin/keys"), gate.resolve(),
                      self.config)

    def test_public_key_cannot_read_private_conversation_history(self):
        token, _rec = self.keystore.create(allow_public=True)
        gate = self._gate({"Authorization": f"Bearer {token}",
                           "X-Forwarded-For": "public-tunnel"},
                          ("127.0.0.1", 12345))
        for method, path in (("GET", "/conversations"),
                             ("GET", "/conversations/conv-1"),
                             ("GET", "/armor")):
            with self.subTest(path=path), self.assertRaises(ProxyError):
                authorize(match_route(method, path), gate.resolve(), self.config)

    def test_operator_admin_header(self):
        admin_key = "triad-admin-test-key"
        self.admin_hashes.add(hashlib.sha256(admin_key.encode()).hexdigest())
        identity = self._gate({"Authorization": f"Bearer {admin_key}"}, ("192.168.1.1", 12345)).resolve()
        self.assertEqual(identity.level, "operator")
        self.assertEqual(identity.via, "admin-key")

    def test_operator_session_cookie(self):
        acc = self.accounts.create("alice", "pw")
        sid = self.accounts.create_session(acc.uid)
        identity = self._gate({"Cookie": f"triad_sid={sid}"}, ("192.168.1.1", 12345)).resolve()
        self.assertEqual(identity.level, "operator")
        self.assertEqual(identity.user_id, acc.uid)

    def test_browser_detected(self):
        identity = self._gate({
            "User-Agent": "Mozilla/5.0",
            "Sec-Fetch-Site": "same-origin",
        }, ("10.0.0.1", 12345)).resolve()
        self.assertEqual(identity.level, "browser")

    def test_unknown_is_none(self):
        identity = self._gate({}, ("10.0.0.1", 12345)).resolve()
        self.assertEqual(identity.level, "none")

    def test_empty_bearer_not_client_key(self):
        identity = self._gate({"Authorization": "Bearer "}, ("127.0.0.1", 12345)).resolve()
        self.assertEqual(identity.level, "loopback")

    def test_same_origin_local_browser_is_operator_without_login(self):
        identity = self._gate({
            "Host": "127.0.0.1:8412",
            "Origin": "http://127.0.0.1:8412",
            "Sec-Fetch-Site": "same-origin",
        }, ("127.0.0.1", 12345)).resolve()
        self.assertEqual(identity.level, "operator")
        self.assertEqual(identity.via, "local")

    def test_cross_origin_browser_on_loopback_is_not_operator(self):
        identity = self._gate({
            "Host": "127.0.0.1:8412",
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
        }, ("127.0.0.1", 12345)).resolve()
        self.assertNotEqual(identity.level, "operator")

    def test_forwarded_loopback_browser_is_not_operator(self):
        identity = self._gate({
            "Host": "127.0.0.1:8412",
            "Origin": "http://127.0.0.1:8412",
            "Sec-Fetch-Site": "same-origin",
            "CF-Connecting-IP": "203.0.113.5",
        }, ("127.0.0.1", 12345)).resolve()
        self.assertNotEqual(identity.level, "operator")

    def test_public_listener_does_not_get_passwordless_admin(self):
        self.config.bind_host = "0.0.0.0"
        identity = self._gate({
            "Host": "127.0.0.1:8412",
            "Origin": "http://127.0.0.1:8412",
            "Sec-Fetch-Site": "same-origin",
        }, ("127.0.0.1", 12345)).resolve()
        self.assertNotEqual(identity.level, "operator")


if __name__ == "__main__":
    unittest.main()


class SetupKeyOperatorTests(unittest.TestCase):
    """The first-run key is the operator's credential — but only on a fresh install.

    Without the owner-account condition this would let any key that setup ever
    issued administer the proxy, which is a privilege escalation rather than a
    convenience.
    """

    def _gate(self, token, *, with_owner):
        from popo.auth.gate import Gate
        from popo.config import Config
        config = Config(require_client_key=True)
        keystore = KeyStore("", EphemeralGate())
        accounts = AccountStore("", EphemeralGate())
        if with_owner:
            accounts.create("owner", "pw")
        typed, record = keystore.create(label="quick-setup")
        return Gate(config, {"Authorization": f"Bearer {typed}"},
                    ("127.0.0.1", 1), keystore=keystore, accounts=accounts,
                    admin_hashes=set()), typed

    def test_setup_key_is_operator_on_fresh_install(self):
        gate, _token = self._gate(None, with_owner=False)
        self.assertEqual(gate.resolve().level, "operator")

    def test_setup_key_is_not_operator_once_an_owner_exists(self):
        gate, _token = self._gate(None, with_owner=True)
        self.assertEqual(gate.resolve().level, "client-key")

    def test_public_setup_key_does_not_become_operator_through_tunnel(self):
        from popo.auth.gate import Gate
        from popo.config import Config
        keystore = KeyStore("", EphemeralGate())
        accounts = AccountStore("", EphemeralGate())
        token, _record = keystore.create(label="quick-setup", allow_public=True)
        gate = Gate(Config(), {"Authorization": f"Bearer {token}",
                               "X-Forwarded-For": "public-tunnel"},
                    ("127.0.0.1", 1), keystore=keystore, accounts=accounts,
                    admin_hashes=set())
        identity = gate.resolve()
        self.assertEqual(identity.level, "client-key")
        self.assertTrue(identity.allow_public)

    def test_ordinary_client_key_is_never_operator(self):
        from popo.auth.gate import Gate
        from popo.config import Config
        keystore = KeyStore("", EphemeralGate())
        accounts = AccountStore("", EphemeralGate())
        token, _ = keystore.create(label="someone-else")
        gate = Gate(Config(), {"Authorization": f"Bearer {token}"}, ("127.0.0.1", 1),
                    keystore=keystore, accounts=accounts, admin_hashes=set())
        self.assertEqual(gate.resolve().level, "client-key")
