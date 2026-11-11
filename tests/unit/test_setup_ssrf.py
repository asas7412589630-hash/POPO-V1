"""The setup probe must not be usable as a request forwarder.

The failure these tests lock down is ordering: if the token check happens after
the network probe, an anonymous caller can make the proxy connect to arbitrary
addresses — loopback services, the LAN, and cloud metadata endpoints — even
though the request is ultimately rejected.

Two properties are asserted separately:

* ``egress.validate`` refuses private/link-local/metadata destinations;
* ``SetupService`` checks authorisation *before* touching the network, verified
  by a probe spy that fails the test if it is ever called.
"""
from __future__ import annotations
import json
import tempfile
import unittest

from popo.auth.bootstrap import BootstrapToken
from popo.auth.keys import KeyStore
from popo.config import Config
from popo.errors import ProxyError
from popo.server import egress
from popo.server.setup import SetupService, SetupStore
from popo.storage.ephemeral import EphemeralGate


class EgressPolicyTests(unittest.TestCase):
    def test_loopback_literal_rejected(self):
        with self.assertRaises(egress.EgressError):
            egress.validate("http://127.0.0.1:8412/v1")

    def test_metadata_endpoint_rejected(self):
        for url in ("http://169.254.169.254/latest/meta-data",
                    "http://metadata.google.internal/v1",
                    "http://[::1]:80/v1"):
            with self.assertRaises(egress.EgressError, msg=url):
                egress.validate(url)

    def test_private_ranges_rejected(self):
        for url in ("http://10.0.0.5/v1", "http://192.168.1.10/v1",
                    "http://172.16.4.4/v1", "http://100.64.1.1/v1"):
            with self.assertRaises(egress.EgressError, msg=url):
                egress.validate(url)

    def test_non_http_scheme_rejected(self):
        for url in ("file:///etc/passwd", "gopher://example.com/", "ftp://x/y"):
            with self.assertRaises(egress.EgressError, msg=url):
                egress.validate(url)

    def test_public_literal_allowed(self):
        self.assertEqual(egress.validate("https://1.1.1.1/v1"), "https://1.1.1.1/v1")

    def test_private_allowed_only_on_explicit_opt_in(self):
        self.assertEqual(
            egress.validate("http://127.0.0.1:11434/v1", allow_private=True),
            "http://127.0.0.1:11434/v1")

    def test_missing_host_rejected(self):
        with self.assertRaises(egress.EgressError):
            egress.validate("https:///v1")

    def test_resolved_private_name_rejected(self):
        # A name that resolves to loopback must be refused even though the
        # literal looks public. Checked without network by inspecting the helper.
        if not egress._resolve("localhost"):
            self.skipTest("localhost does not resolve in this sandbox")
        with self.assertRaises(egress.EgressError):
            egress.validate("http://localhost:11434/v1")


class _ProbeSpy:
    """Records whether a probe ran, and fails loudly if it should not have."""

    def __init__(self):
        self.called = 0

    def __call__(self, *args, **kwargs):
        self.called += 1
        raise AssertionError("network probe ran before authorisation")


def _service(allow_private: bool = False, with_bootstrap: bool = True,
             preexisting_key: bool = False):
    tmp = tempfile.mkdtemp(prefix="popo_ssrf_")
    config = Config(data_dir=tmp, upstream_host="", upstream_key="",
                    published_models=[], allow_private_upstream=allow_private)
    store = SetupStore(tmp, persist=False)
    keystore = KeyStore("", EphemeralGate())
    if preexisting_key:
        keystore.create(label="existing")
    token = BootstrapToken(tmp) if with_bootstrap else None
    service = SetupService(config, store, keystore, token)
    return service, token, keystore


def _payload(host: str, token: str = "", key: str = "sk-test") -> bytes:
    body = {"upstream_host": host, "upstream_key": key, "models": ["m"]}
    if token:
        body["setup_token"] = token
    return json.dumps(body).encode()


class AuthorizationOrderTests(unittest.TestCase):
    def test_quick_does_not_probe_before_token_check(self):
        service, _token, _ = _service()
        spy = _ProbeSpy()
        service._probe_models = spy
        service._probe_chat = spy
        with self.assertRaises(ProxyError) as ctx:
            service.quick(_payload("https://api.example.invalid/v1"))
        self.assertEqual(ctx.exception.kind.value, "client.unauthorized")
        self.assertEqual(spy.called, 0, "probe ran before the token was checked")

    def test_test_endpoint_does_not_probe_before_token_check(self):
        service, _token, _ = _service()
        spy = _ProbeSpy()
        service._probe_models = spy
        service._probe_chat = spy
        with self.assertRaises(ProxyError):
            service.test(_payload("https://api.example.invalid/v1"))
        self.assertEqual(spy.called, 0)

    def test_list_models_does_not_probe_before_token_check(self):
        service, _token, _ = _service()
        spy = _ProbeSpy()
        service._probe_models = spy
        with self.assertRaises(ProxyError):
            service.list_upstream_models(_payload("https://api.example.invalid/v1"))
        self.assertEqual(spy.called, 0)

    def test_authorised_request_still_cannot_probe_loopback(self):
        service, token, _ = _service()
        spy = _ProbeSpy()
        service._probe_models = spy
        service._probe_chat = spy
        plaintext = token.issue()
        with self.assertRaises(ProxyError) as ctx:
            service.quick(_payload("http://127.0.0.1:8412/v1", plaintext))
        self.assertEqual(ctx.exception.kind.value, "client.bad_request")
        self.assertIn("private/loopback", ctx.exception.message)
        self.assertEqual(spy.called, 0, "destination check must precede the probe")

    def test_configured_install_cannot_be_pointed_at_loopback_either(self):
        # Once configured the token is no longer required, but the destination
        # policy still applies: the SSRF primitive must not reappear.
        service, _token, _ = _service(preexisting_key=True)
        spy = _ProbeSpy()
        service._probe_models = spy
        with self.assertRaises(ProxyError) as ctx:
            service.quick(_payload("http://169.254.169.254/latest"))
        self.assertEqual(ctx.exception.kind.value, "client.bad_request")
        self.assertEqual(spy.called, 0)

    def test_opt_in_allows_local_upstream(self):
        service, token, _ = _service(allow_private=True)
        service._probe_models = lambda base, key: ["m"]
        service._probe_chat = lambda base, key, model: None
        plaintext = token.issue()
        status, body = service.quick(_payload("http://127.0.0.1:11434/v1", plaintext))
        self.assertEqual(status, 200)
        self.assertTrue(body["key"].startswith("triad-"))


if __name__ == "__main__":
    unittest.main()
