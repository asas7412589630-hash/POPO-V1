from __future__ import annotations

import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from popo.config import Config
from popo.server.console_routes import ConsoleRoutes, _is_public_base


class _Writer:
    def __init__(self):
        self.payload = None

    def json(self, _status, payload):
        self.payload = payload


class PublicUrlScopeTests(unittest.TestCase):
    def test_internal_literal_addresses_are_not_public_urls(self):
        for base in (
            "http://127.0.0.1:8412",
            "http://192.168.1.20:8412",
            "http://10.0.0.2:8412",
            "http://[::1]:8412",
            "http://popo.local:8412",
        ):
            with self.subTest(base=base):
                self.assertFalse(_is_public_base(base))

    def test_hostname_resolving_to_private_address_is_not_public(self):
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 443))]
        with patch("popo.server.console_routes.socket.getaddrinfo", return_value=rows):
            self.assertFalse(_is_public_base("https://api.example.com"))

    def test_public_hostname_must_resolve_only_to_public_addresses(self):
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        with patch("popo.server.console_routes.socket.getaddrinfo", return_value=rows):
            self.assertTrue(_is_public_base("https://api.example.com"))

    def test_access_endpoint_never_labels_loopback_as_public(self):
        with tempfile.TemporaryDirectory(prefix="popo-public-url-") as data_dir:
            route = object.__new__(ConsoleRoutes)
            route.cfg = Config(bind_host="127.0.0.1", bind_port=8412,
                               data_dir=data_dir,
                               public_base="http://127.0.0.1:8412")
            writer = _Writer()
            route._writer = lambda: writer
            with patch("popo.server.console_routes._probe_public_base",
                       return_value=True) as probe:
                route._route_access(b"", SimpleNamespace(query=""), None)

        self.assertEqual(writer.payload["public_base"], "")
        self.assertTrue(writer.payload["public_configured"])
        self.assertFalse(any(item["kind"] == "public"
                             for item in writer.payload["addresses"]))
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
