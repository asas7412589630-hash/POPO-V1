from __future__ import annotations
import threading
import time
import unittest

from popo.errors import ProxyError
from popo.server.limits import ConcurrencyLimit


class ConcurrencyLimitTests(unittest.TestCase):
    def test_allows_up_to_limit(self):
        limiter = ConcurrencyLimit(2, wait_seconds=0.01)
        with limiter.slot():
            with limiter.slot():
                self.assertEqual(limiter.stats()["in_flight"], 2)

    def test_rejects_beyond_limit(self):
        limiter = ConcurrencyLimit(1, wait_seconds=0)
        with limiter.slot():
            with self.assertRaises(ProxyError) as ctx:
                with limiter.slot():
                    pass
        self.assertEqual(ctx.exception.kind.value, "client.rate_limited")

    def test_slot_released_after_exception(self):
        limiter = ConcurrencyLimit(1, wait_seconds=0)
        with self.assertRaises(RuntimeError):
            with limiter.slot():
                raise RuntimeError("boom")
        self.assertEqual(limiter.stats()["in_flight"], 0)
        with limiter.slot():          # must not raise: the slot came back
            pass

    def test_peak_and_rejection_counters(self):
        limiter = ConcurrencyLimit(1, wait_seconds=0)
        with limiter.slot():
            try:
                with limiter.slot():
                    pass
            except ProxyError:
                pass
        stats = limiter.stats()
        self.assertEqual(stats["peak"], 1)
        self.assertEqual(stats["rejected"], 1)

    def test_wait_then_acquire_when_slot_frees(self):
        limiter = ConcurrencyLimit(1, wait_seconds=2.0)
        released = threading.Event()
        holder_started = threading.Event()

        def holder():
            with limiter.slot():
                holder_started.set()
                released.wait(1.0)

        thread = threading.Thread(target=holder)
        thread.start()
        holder_started.wait(1.0)
        threading.Timer(0.2, released.set).start()
        started = time.time()
        with limiter.slot():          # waits for the holder to finish
            pass
        self.assertLess(time.time() - started, 2.0)
        thread.join()

    def test_timeout_is_bounded(self):
        limiter = ConcurrencyLimit(1, wait_seconds=0.1)
        with limiter.slot():
            started = time.time()
            with self.assertRaises(ProxyError):
                with limiter.slot():
                    pass
        self.assertLess(time.time() - started, 1.0)

    def test_concurrent_threads_never_exceed_limit(self):
        limit = 3
        limiter = ConcurrencyLimit(limit, wait_seconds=2.0)
        seen = []
        lock = threading.Lock()

        def worker():
            try:
                with limiter.slot():
                    with lock:
                        seen.append(limiter.stats()["in_flight"])
                    time.sleep(0.02)
            except ProxyError:
                pass

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertLessEqual(max(seen), limit, "limit was exceeded")
        self.assertEqual(limiter.stats()["in_flight"], 0, "slots leaked")

    def test_limit_is_at_least_one(self):
        self.assertEqual(ConcurrencyLimit(0).limit, 1)


if __name__ == "__main__":
    unittest.main()
