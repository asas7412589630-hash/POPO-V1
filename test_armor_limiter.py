#!/usr/bin/env python3
"""Unit tests for armor_limiter module.

Direct verification of RateLimiter sliding windows, path exclusion/prefix
rules, rate limit enforcement, and memory leak eviction pruning.
"""

import json
import os
import tempfile
import time
import unittest

from armor_limiter import RateLimiter


class TestArmorLimiter(unittest.TestCase):
    def setUp(self):
        self.tmp_fd, self.tmp_path = tempfile.mkstemp(suffix=".json")
        os.close(self.tmp_fd)

    def tearDown(self):
        try:
            os.unlink(self.tmp_path)
        except OSError:
            pass

    def test_disabled_by_default_or_missing(self):
        limiter = RateLimiter(self.tmp_path)
        self.assertFalse(limiter.enabled())
        ok, win = limiter.check("client1", "/v1/chat/completions")
        self.assertTrue(ok)
        self.assertEqual(win, "-")

    def test_rate_limit_minute_quota(self):
        cfg = {
            "enabled": True,
            "scopes": {
                "global": {
                    "per_minute": 3,
                    "per_hour": 100,
                    "per_day": 1000,
                }
            },
            "paths": {
                "apply_prefixes": ["/v1/"],
                "exclude_exact": ["/v1/models"],
            }
        }
        with open(self.tmp_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

        limiter = RateLimiter(self.tmp_path)
        self.assertTrue(limiter.enabled())

        # Excluded exact path should always pass
        for _ in range(5):
            ok, win = limiter.check("client1", "/v1/models")
            self.assertTrue(ok)
            self.assertEqual(win, "-")

        # First 3 requests should pass
        for i in range(3):
            ok, win = limiter.check("client1", "/v1/chat/completions")
            self.assertTrue(ok, f"Request {i+1} should pass")
            self.assertEqual(win, "-")

        # 4th request must fail rate limit for minute window
        ok, win = limiter.check("client1", "/v1/chat/completions")
        self.assertFalse(ok)
        self.assertEqual(win, "minute")

        # Different client should still have their quota
        ok2, win2 = limiter.check("client2", "/v1/chat/completions")
        self.assertTrue(ok2)

    def test_memory_eviction_pruning(self):
        limiter = RateLimiter(self.tmp_path, prune_interval=1.0)
        now = time.time()
        # Insert expired timestamps for an ephemeral client
        limiter.state["ephemeral_old"] = {
            "minute": [now - 120],
            "hour": [now - 7200],
            "day": [now - 90000],
        }
        # Insert active timestamps for an active client
        limiter.state["active_user"] = {
            "minute": [now - 5],
            "hour": [now - 100],
            "day": [now - 1000],
        }

        # Force prune
        evicted = limiter.prune(now=now, force=True)
        self.assertEqual(evicted, 1)
        self.assertNotIn("ephemeral_old", limiter.state)
        self.assertIn("active_user", limiter.state)


if __name__ == "__main__":
    unittest.main()
