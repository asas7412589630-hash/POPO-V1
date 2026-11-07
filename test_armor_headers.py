#!/usr/bin/env python3
"""Unit tests for armor_headers module.

Direct verification of header normalization, WAF bot mitigation, edge-block
heuristics, loopback detection, and client IP extraction.
"""

import unittest
import armor_headers as AH


class TestArmorHeaders(unittest.TestCase):
    def test_normalise_bot_user_agent(self):
        bot_uas = [
            "python-requests/2.31.0",
            "curl/7.68.0",
            "Wget/1.20.3 (linux-gnu)",
            "Go-http-client/1.1",
            "python-urllib3/1.26.15",
        ]
        for ua in bot_uas:
            hdrs = {"User-Agent": ua}
            AH._normalise_upstream_headers(hdrs)
            self.assertEqual(hdrs["User-Agent"], AH._DEFAULT_UA)
            self.assertIn("Accept-Language", hdrs)
            self.assertIn("Accept", hdrs)

    def test_preserve_legitimate_custom_ua(self):
        hdrs = {"User-Agent": "MyCustomAgent/1.0", "Accept": "application/json"}
        AH._normalise_upstream_headers(hdrs)
        self.assertEqual(hdrs["User-Agent"], "MyCustomAgent/1.0")
        self.assertEqual(hdrs["Accept"], "application/json")
        self.assertIn("Accept-Language", hdrs)

    def test_looks_like_edge_block_cloudflare(self):
        # cf-mitigated header
        self.assertTrue(AH._looks_like_edge_block(403, {"cf-mitigated": "challenge"}, b""))
        # html challenge body
        body = b"<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title></head></html>"
        self.assertTrue(AH._looks_like_edge_block(403, {}, body))
        self.assertTrue(AH._looks_like_edge_block(503, {}, b"<html>Ray ID: 123456 cf-error</html>"))

    def test_looks_like_edge_block_negative(self):
        # 200 is never edge block
        self.assertFalse(AH._looks_like_edge_block(200, {}, b"<html>ok</html>"))
        # Standard OpenAI API JSON 400/404 error is not an edge block
        self.assertFalse(AH._looks_like_edge_block(404, {}, b'{"error":{"message":"model not found"}}'))

    def test_is_loopback(self):
        self.assertTrue(AH._is_loopback("127.0.0.1"))
        self.assertTrue(AH._is_loopback("127.0.1.1"))
        self.assertTrue(AH._is_loopback("::1"))
        self.assertTrue(AH._is_loopback("localhost"))
        self.assertTrue(AH._is_loopback(""))
        self.assertFalse(AH._is_loopback("192.168.1.100"))
        self.assertFalse(AH._is_loopback("8.8.8.8"))

    def test_forwarded_client(self):
        self.assertEqual(AH._forwarded_client({"CF-Connecting-IP": "203.0.113.19"}), "203.0.113.19")
        self.assertEqual(AH._forwarded_client({"X-Forwarded-For": "203.0.113.50, 10.0.0.1"}), "203.0.113.50")
        self.assertEqual(AH._forwarded_client({"X-Real-IP": "198.51.100.2"}), "198.51.100.2")

    def test_rid_now_format(self):
        rid1 = AH._rid_now()
        rid2 = AH._rid_now()
        self.assertTrue(rid1.isdigit())
        self.assertTrue(int(rid2) >= int(rid1))


if __name__ == "__main__":
    unittest.main()
