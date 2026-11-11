from __future__ import annotations
import importlib.util, pathlib, unittest

ROOT = pathlib.Path('/var/hark/shared/POPO-V2')

class TokenEndpointTests(unittest.TestCase):
    """The console must be able to obtain the first-run token, and a web page must not."""

    def setUp(self):
        import tempfile
        from popo.config import Config
        from popo.server.app import ApiHandler
        self.tmp = tempfile.mkdtemp(prefix="popo_token_")
        self.handler_cls = ApiHandler
        self.saved = (ApiHandler.config, ApiHandler.keystore, ApiHandler.records,
                      ApiHandler.accounts, ApiHandler.admin_hashes,
                      getattr(ApiHandler, "_bootstrap_token", None))
        ApiHandler.config = Config(data_dir=self.tmp, require_client_key=True)
        ApiHandler.keystore = None
        ApiHandler.records = None
        ApiHandler.accounts = None
        ApiHandler.admin_hashes = set()
        ApiHandler._bootstrap_token = None

    def tearDown(self):
        (self.handler_cls.config, self.handler_cls.keystore, self.handler_cls.records,
         self.handler_cls.accounts, self.handler_cls.admin_hashes,
         self.handler_cls._bootstrap_token) = self.saved

    def _fake(self, headers, addr):
        from popo.server import deps
        h = object.__new__(self.handler_cls)
        object.__setattr__(h, "headers", headers)
        object.__setattr__(h, "client_address", addr)
        return h

    def test_loopback_terminal_gets_a_token(self):
        from popo.server import deps
        h = self._fake({}, ("127.0.0.1", 5000))
        self.assertTrue(deps.ensure_bootstrap(h).startswith("setup-"))

    def test_browser_page_is_refused(self):
        from popo.server import deps
        h = self._fake({"Origin": "http://127.0.0.1:8412"}, ("127.0.0.1", 5000))
        self.assertEqual(deps.ensure_bootstrap(h), "")
        h2 = self._fake({"Origin": "http://evil.example"}, ("127.0.0.1", 5000))
        self.assertEqual(deps.ensure_bootstrap(h2), "")

    def test_remote_peer_gets_nothing(self):
        from popo.server import deps
        h = self._fake({}, ("10.0.0.9", 5000))
        self.assertEqual(deps.ensure_bootstrap(h), "")

    def test_configured_install_has_no_token(self):
        from popo.server import deps
        deps.keystore_of(self._fake({}, ("127.0.0.1", 1))).create(label="x")
        h = self._fake({}, ("127.0.0.1", 5000))
        self.assertEqual(deps.ensure_bootstrap(h), "")

if __name__ == "__main__":
    unittest.main()


class TunnelIsNotLocalTests(unittest.TestCase):
    """A tunnel client connects from 127.0.0.1 while the caller is remote.

    Trusting the peer address alone would give the internet key-free access to a
    published proxy — and to the first-run token, which is worse.
    """

    def _gate(self, headers):
        from popo.auth.gate import Gate
        from popo.config import Config
        return Gate(Config(require_client_key=True, loopback_trusted=True),
                    headers, ("127.0.0.1", 5000))

    def test_plain_loopback_is_a_local_tool(self):
        self.assertTrue(self._gate({}).is_local_tool())

    def test_forwarded_header_disqualifies(self):
        self.assertFalse(self._gate({"X-Forwarded-For": "203.0.113.7"}).is_local_tool())
        self.assertFalse(self._gate({"CF-Connecting-IP": "203.0.113.7"}).is_local_tool())
        self.assertFalse(self._gate({"CF-Ray": "abc123"}).is_local_tool())

    def test_browser_marker_disqualifies(self):
        self.assertFalse(self._gate({"Origin": "http://x"}).is_local_tool())

    def test_remote_peer_is_not_local(self):
        from popo.auth.gate import Gate
        from popo.config import Config
        gate = Gate(Config(require_client_key=True), {}, ("10.0.0.5", 1))
        self.assertFalse(gate.is_local_tool())

    def test_tunnel_peer_is_not_trusted_as_loopback_level(self):
        from popo.auth.gate import Gate
        from popo.config import Config
        gate = Gate(Config(require_client_key=True, loopback_trusted=True),
                    {"CF-Connecting-IP": "203.0.113.7"}, ("127.0.0.1", 5000))
        self.assertNotEqual(gate.resolve().level, "loopback")
