"""A 429 and an empty balance need opposite handling.

The single ``upstream.capacity`` bucket made the proxy either wait when it should
stop, or stop when it should wait. These tests pin the distinctions.
"""
from __future__ import annotations
import unittest

from popo.core.upstream import UpstreamSource
from popo.errors import ErrorKind, classify_status


def kind(status, body=b"", headers=None):
    return UpstreamSource._error_for(status, body, headers or {}).kind


class FailureTaxonomyTests(unittest.TestCase):
    def test_429_with_retry_after_says_how_long(self):
        err = UpstreamSource._error_for(429, b'{"error":"slow"}', {"Retry-After": "30"})
        self.assertEqual(err.kind, ErrorKind.UPSTREAM_RATE_LIMITED)
        self.assertIn("30", err.message)

    def test_429_without_header_is_still_rate_limit(self):
        self.assertEqual(kind(429, b'{"error":"rate"}'), ErrorKind.UPSTREAM_RATE_LIMITED)

    def test_quota_exhaustion_is_not_a_rate_limit(self):
        self.assertEqual(kind(429, b'{"error":{"code":"insufficient_quota"}}'),
                         ErrorKind.UPSTREAM_QUOTA_EXHAUSTED)

    def test_402_is_quota(self):
        self.assertEqual(kind(402, b"payment required"), ErrorKind.UPSTREAM_QUOTA_EXHAUSTED)

    def test_quota_is_not_retryable(self):
        _http, retryable = classify_status(
            UpstreamSource._error_for(429, b'"insufficient_quota"', {}))
        self.assertFalse(retryable)

    def test_rate_limit_is_retryable(self):
        _http, retryable = classify_status(
            UpstreamSource._error_for(429, b'{"error":"rate"}', {}))
        self.assertTrue(retryable)

    def test_plain_503_is_overloaded_not_edge(self):
        self.assertEqual(kind(503, b"<html>bad gateway</html>"),
                         ErrorKind.UPSTREAM_OVERLOADED)

    def test_named_edge_marker_is_edge(self):
        body = b"<title>Attention Required! | Cloudflare</title>"
        self.assertEqual(kind(403, body), ErrorKind.UPSTREAM_EDGE_BLOCKED)

    def test_cf_mitigated_header_is_edge(self):
        self.assertEqual(kind(403, b"{}", {"cf-mitigated": "challenge"}),
                         ErrorKind.UPSTREAM_EDGE_BLOCKED)

    def test_401_is_auth(self):
        self.assertEqual(kind(401, b'{"error":"bad key"}'), ErrorKind.UPSTREAM_AUTH)

    def test_404_is_model_not_found(self):
        self.assertEqual(kind(404, b'{"error":"no model"}'),
                         ErrorKind.UPSTREAM_MODEL_NOT_FOUND)

    def test_402_429_503_do_not_collapse(self):
        codes = {classify_status(UpstreamSource._error_for(s, b"", {}))[0]
                 for s in (402, 429, 503)}
        self.assertEqual(len(codes), 3, "402/429/503 must not share one status")


class KeyRotationPolicyTests(unittest.TestCase):
    def test_quota_does_not_rotate_keys(self):
        err = UpstreamSource._error_for(429, b'"insufficient_quota"', {})
        self.assertFalse(UpstreamSource._retryable(err))

    def test_auth_does_rotate_keys(self):
        err = UpstreamSource._error_for(401, b"", {})
        self.assertTrue(UpstreamSource._retryable(err))

    def test_overload_does_rotate_keys(self):
        err = UpstreamSource._error_for(503, b"", {})
        self.assertTrue(UpstreamSource._retryable(err))


if __name__ == "__main__":
    unittest.main()
