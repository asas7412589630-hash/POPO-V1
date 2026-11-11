from __future__ import annotations
import time
import unittest

from popo.auth.limiter import AttemptLimiter


class AttemptLimiterTests(unittest.TestCase):
    def test_allows_until_limit(self):
        limiter = AttemptLimiter(max_attempts=3, window_seconds=60)
        for _ in range(3):
            self.assertTrue(limiter.allowed("1.2.3.4"))
            limiter.record_failure("1.2.3.4")
        self.assertFalse(limiter.allowed("1.2.3.4"))

    def test_identities_are_independent(self):
        limiter = AttemptLimiter(max_attempts=1, window_seconds=60)
        limiter.record_failure("10.0.0.1")
        self.assertFalse(limiter.allowed("10.0.0.1"))
        self.assertTrue(limiter.allowed("10.0.0.2"))

    def test_window_expiry_frees_the_caller(self):
        limiter = AttemptLimiter(max_attempts=1, window_seconds=0.05)
        limiter.record_failure("10.0.0.3")
        self.assertFalse(limiter.allowed("10.0.0.3"))
        time.sleep(0.1)
        self.assertTrue(limiter.allowed("10.0.0.3"))

    def test_reset_clears_history(self):
        limiter = AttemptLimiter(max_attempts=1, window_seconds=60)
        limiter.record_failure("10.0.0.4")
        limiter.reset("10.0.0.4")
        self.assertTrue(limiter.allowed("10.0.0.4"))

    def test_table_is_bounded(self):
        limiter = AttemptLimiter(max_attempts=5, window_seconds=60, max_keys=32)
        for index in range(500):
            limiter.record_failure(f"10.1.0.{index}")
        self.assertLessEqual(len(limiter._hits), 32)

    def test_retry_after_is_reported(self):
        limiter = AttemptLimiter(max_attempts=1, window_seconds=60)
        limiter.record_failure("10.0.0.5")
        self.assertGreater(limiter.retry_after("10.0.0.5"), 0)


if __name__ == "__main__":
    unittest.main()
