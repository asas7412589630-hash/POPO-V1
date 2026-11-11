from __future__ import annotations
import unittest

from popo.server.deploy import (TRUSTED_SUFFIXES, discover_tunnel, env_file_template,
                                systemd_unit, unit_contains_secret)


class TunnelDiscoveryTests(unittest.TestCase):
    def test_picks_trusted_url_from_log(self):
        log = "Connected 1: https://abc123.srv.us/\nother noise"
        candidate = discover_tunnel([(log, "log")])
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.url, "https://abc123.srv.us")
        self.assertTrue(candidate.trusted)

    def test_ignores_untrusted_host(self):
        self.assertIsNone(discover_tunnel([("https://evil.example", "log")]))

    def test_ignores_lookalike_suffix(self):
        # A domain that merely contains the suffix must not pass.
        self.assertIsNone(discover_tunnel([("https://srv.us.evil.example", "log")]))

    def test_priority_order_respected(self):
        candidate = discover_tunnel([("https://one.srv.us", "file"),
                                     ("https://two.srv.us", "log")])
        self.assertEqual(candidate.url, "https://one.srv.us")
        self.assertEqual(candidate.source, "file")

    def test_falls_through_to_next_source(self):
        candidate = discover_tunnel([("no url here", "file"),
                                     ("https://x.trycloudflare.com", "log")])
        self.assertEqual(candidate.url, "https://x.trycloudflare.com")

    def test_returns_none_rather_than_guessing(self):
        self.assertIsNone(discover_tunnel([("", "file"), ("http://127.0.0.1:8412", "log")]))

    def test_known_suffixes_are_declared(self):
        self.assertIn("srv.us", TRUSTED_SUFFIXES)


class SystemdUnitTests(unittest.TestCase):
    def test_unit_has_no_inline_credential(self):
        self.assertFalse(unit_contains_secret(systemd_unit(workdir="/opt/popo")))

    def test_unit_uses_environment_file(self):
        unit = systemd_unit(workdir="/opt/popo", env_file="/etc/popo/popo.env")
        self.assertIn("EnvironmentFile=/etc/popo/popo.env", unit)

    def test_unit_sets_bind_and_port(self):
        unit = systemd_unit(workdir="/opt/popo", port=9000, bind="127.0.0.1")
        self.assertIn("Environment=POPO_PORT=9000", unit)
        self.assertIn("Environment=POPO_BIND=127.0.0.1", unit)

    def test_unit_writes_only_its_own_directories(self):
        unit = systemd_unit(workdir="/opt/popo")
        self.assertIn("ReadWritePaths=/opt/popo/data /opt/popo/logs", unit)
        self.assertIn("ProtectSystem=strict", unit)

    def test_unit_has_install_target(self):
        self.assertIn("WantedBy=multi-user.target", systemd_unit(workdir="/opt/popo"))

    def test_optional_user_is_emitted(self):
        self.assertIn("User=popo", systemd_unit(workdir="/opt/popo", user="popo"))

    def test_env_template_has_placeholders_only(self):
        template = env_file_template()
        self.assertIn("sk-replace-me", template)
        self.assertNotRegex(template, r"sk-[A-Za-z0-9]{20,}")


if __name__ == "__main__":
    unittest.main()
