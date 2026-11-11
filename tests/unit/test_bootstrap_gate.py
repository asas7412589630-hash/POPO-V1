from __future__ import annotations
import json
import tempfile
import unittest

from popo.auth.bootstrap import BootstrapToken
from popo.auth.keys import KeyStore
from popo.config import Config
from popo.errors import ProxyError
from popo.server.setup import SetupService, SetupStore
from popo.storage.ephemeral import EphemeralGate


def _service(bootstrap: bool = True):
    tmp = tempfile.mkdtemp(prefix="popo_gate_")
    config = Config(data_dir=tmp, upstream_host="", upstream_key="",
                    bind_port=8412, published_models=[])
    store = SetupStore(tmp, persist=False)
    keystore = KeyStore("", EphemeralGate())
    token = BootstrapToken(tmp) if bootstrap else None
    service = SetupService(config, store, keystore, token)
    # Replace the network probes: this test is about the gate, not about HTTP.
    service._probe_models = lambda base, key: ["glm-5.3"]
    service._probe_chat = lambda base, key, model: None
    return service, token, keystore


def _payload(token: str = "") -> bytes:
    body = {"upstream_host": "https://api.example.invalid/v1",
            "upstream_key": "sk-test", "models": ["glm-5.3"]}
    if token:
        body["setup_token"] = token
    return json.dumps(body).encode()


class BootstrapGateTests(unittest.TestCase):
    def test_fresh_install_refuses_without_token(self):
        service, _token, _ = _service()
        self.assertFalse(service.keystore.list_all())
        with self.assertRaises(ProxyError) as ctx:
            service.quick(_payload())
        self.assertEqual(ctx.exception.kind.value, "client.unauthorized")
        self.assertIn("setup_token", ctx.exception.message)

    def test_fresh_install_accepts_valid_token(self):
        service, token, keystore = _service()
        plaintext = token.issue()
        status, body = service.quick(_payload(plaintext))
        self.assertEqual(status, 200)
        self.assertTrue(body["key"].startswith("triad-"))
        self.assertTrue(keystore.list_all())

    def test_existing_install_does_not_need_token(self):
        service, _token, keystore = _service()
        keystore.create(label="existing")
        status, _body = service.quick(_payload())
        self.assertEqual(status, 200)

    def test_wrong_token_rejected(self):
        service, token, _ = _service()
        token.issue()
        with self.assertRaises(ProxyError):
            service.quick(_payload("setup-wrong"))

    def test_consumed_token_cannot_be_reused(self):
        service, token, _ = _service()
        plaintext = token.issue()
        service.quick(_payload(plaintext))
        self.assertFalse(token.active)
        self.assertFalse(token.consume(plaintext))
        # A second fresh install reading the same state must not accept it either.
        _, counterpart, _ = _service()
        counterpart._digest = token._digest
        counterpart._expires = token._expires
        self.assertFalse(counterpart.verify(plaintext))

    def test_second_quick_on_configured_install_needs_no_token(self):
        # Once a key exists the install is no longer fresh; demanding the token
        # again would lock out a working installation.
        service, token, _ = _service()
        service.quick(_payload(token.issue()))
        status, _body = service.quick(_payload())
        self.assertEqual(status, 200)

    def test_without_bootstrap_object_behaves_as_before(self):
        service, _token, _ = _service(bootstrap=False)
        status, _body = service.quick(_payload())
        self.assertEqual(status, 200)

    def test_token_not_leaked_in_response(self):
        service, token, _ = _service()
        plaintext = token.issue()
        _status, body = service.quick(_payload(plaintext))
        self.assertNotIn(plaintext, json.dumps(body))


if __name__ == "__main__":
    unittest.main()
