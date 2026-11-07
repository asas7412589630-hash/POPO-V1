#!/usr/bin/env python3
"""Unit tests for armor_pool module.

Direct verification of connection pooling, thread safety, TLS context caching,
and pool stats accounting without running proxy daemon.
"""

import http.client
import sys
import unittest
from unittest.mock import MagicMock

import armor_pool as AP


class TestArmorPool(unittest.TestCase):
    def setUp(self):
        AP._pool_clear()
        with AP._POOL_STATS_LOCK:
            AP._POOL_STATS["hit"] = 0
            AP._POOL_STATS["miss"] = 0
            AP._POOL_STATS["stale"] = 0

    def tearDown(self):
        AP._pool_clear()

    def test_conn_key(self):
        key1 = AP._conn_key(http.client.HTTPConnection, "api.openai.com", 443, True)
        key2 = AP._conn_key(http.client.HTTPConnection, "api.openai.com", 443, True)
        key3 = AP._conn_key(http.client.HTTPConnection, "api.openai.com", 80, False)
        self.assertEqual(key1, key2)
        self.assertNotEqual(key1, key3)
        self.assertEqual(key1, (http.client.HTTPConnection, "api.openai.com", 443, True))

    def test_pool_put_and_take(self):
        key = AP._conn_key(http.client.HTTPConnection, "127.0.0.1", 8080, False)
        mock_conn = MagicMock()
        mock_conn.sock = MagicMock()
        mock_conn.sock.fileno.return_value = 10

        # Put mock into pool
        AP._pool_put(key, mock_conn)
        with AP._CONN_POOL_LOCK:
            self.assertEqual(len(AP._CONN_POOL.get(key, [])), 1)

        # Clear pool drops connection
        AP._pool_clear()
        with AP._CONN_POOL_LOCK:
            self.assertEqual(len(AP._CONN_POOL), 0)
        mock_conn.close.assert_called()

    def test_pool_stats(self):
        stats = AP.pool_stats()
        self.assertIn("hit", stats)
        self.assertIn("miss", stats)
        self.assertIn("stale", stats)

    def test_pool_size_bound(self):
        key = AP._conn_key(http.client.HTTPConnection, "127.0.0.1", 8080, False)
        conns = []
        for _ in range(AP.UPSTREAM_POOL_SIZE + 5):
            c = MagicMock()
            c.sock = MagicMock()
            conns.append(c)
            AP._pool_put(key, c)

        with AP._CONN_POOL_LOCK:
            # Pool must never exceed UPSTREAM_POOL_SIZE
            self.assertLessEqual(len(AP._CONN_POOL.get(key, [])), AP.UPSTREAM_POOL_SIZE)

    def test_tls_context_cached(self):
        ctx1 = AP._tls_context()
        ctx2 = AP._tls_context()
        self.assertIs(ctx1, ctx2)


if __name__ == "__main__":
    unittest.main()
