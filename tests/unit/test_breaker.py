"""A source that keeps failing should stop being called for a while.

Without a breaker, every request pays the timeout of a source that is already
known to be down — the proxy looks slow rather than broken, and the operator gets
no signal about which source is at fault.
"""
from __future__ import annotations
import unittest

from popo.server.sources import Source, SourceCatalog, SourceRouter


def _router() -> SourceRouter:
    catalog = SourceCatalog()
    catalog = catalog.add_source(Source.build("alpha", "A", "https://a.example/v1"))
    catalog = catalog.publish("alpha", ["m"])
    return SourceRouter(catalog, {"alpha": ["k"]})


class BreakerTests(unittest.TestCase):
    def test_healthy_source_is_not_cooling(self):
        self.assertFalse(_router().is_cooling("alpha"))

    def test_trips_after_threshold(self):
        router = _router()
        for _ in range(router.failure_threshold):
            router.mark_failure("alpha")
        self.assertTrue(router.is_cooling("alpha"))

    def test_below_threshold_does_not_trip(self):
        router = _router()
        router.mark_failure("alpha")
        self.assertFalse(router.is_cooling("alpha"))

    def test_success_clears_the_record(self):
        router = _router()
        for _ in range(router.failure_threshold):
            router.mark_failure("alpha")
        router.mark_success("alpha")
        self.assertFalse(router.is_cooling("alpha"))

    def test_cooldown_expires(self):
        router = _router()
        router.cooldown_seconds = 0.01
        for _ in range(router.failure_threshold):
            router.mark_failure("alpha")
        self.assertTrue(router.is_cooling("alpha"))
        import time
        time.sleep(0.05)
        self.assertFalse(router.is_cooling("alpha"),
                         "an expired cooldown must allow a probe")

    def test_unknown_source_is_not_cooling(self):
        self.assertFalse(_router().is_cooling("never-configured"))


if __name__ == "__main__":
    unittest.main()
