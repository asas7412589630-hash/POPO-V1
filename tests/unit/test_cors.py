from __future__ import annotations
import unittest

from popo.server.cors import CorsPolicy


class CorsPolicyTests(unittest.TestCase):
    def test_loopback_console_can_read(self):
        policy = CorsPolicy("127.0.0.1", 8412)
        self.assertTrue(policy.allows("http://127.0.0.1:8412"))
        self.assertIn("Access-Control-Allow-Origin", policy.headers_for("http://127.0.0.1:8412"))

    def test_forged_origin_is_refused(self):
        policy = CorsPolicy("127.0.0.1", 8412)
        self.assertFalse(policy.allows("http://evil.example"))
        self.assertEqual(policy.headers_for("http://evil.example"), {})

    def test_null_origin_refused_by_default(self):
        policy = CorsPolicy("127.0.0.1", 8412)
        self.assertFalse(policy.allows("null"))

    def test_null_origin_can_be_explicitly_allowed(self):
        policy = CorsPolicy("127.0.0.1", 8412, allow_null=True)
        self.assertTrue(policy.allows("null"))

    def test_tunnel_base_is_allowed_but_not_caller_host(self):
        policy = CorsPolicy("127.0.0.1", 8412, public_base="https://abc.srv.us")
        self.assertTrue(policy.allows("https://abc.srv.us"))
        self.assertFalse(policy.allows("https://attacker.srv.us"))

    def test_wildcard_bind_still_grants_loopback_names(self):
        policy = CorsPolicy("0.0.0.0", 9000)
        self.assertTrue(policy.allows("http://127.0.0.1:9000"))
        self.assertTrue(policy.allows("http://localhost:9000"))
        self.assertFalse(policy.allows("http://192.168.1.5:9000"))

    def test_default_port_omitted_in_origin(self):
        policy = CorsPolicy("127.0.0.1", 80)
        self.assertTrue(policy.allows("http://127.0.0.1"))


if __name__ == "__main__":
    unittest.main()
