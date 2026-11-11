"""Session stickiness across sources.

Source A does not know what source B was told. If a conversation switches
providers mid-thread, the model receives a history it never produced, so the
choice must be stable per session — and must not grow without bound.
"""
from __future__ import annotations
import unittest

from popo.server.sources import Source, SourceCatalog, SourceRouter


def _router(sticky_capacity: int = 4096) -> SourceRouter:
    catalog = SourceCatalog()
    catalog = catalog.add_source(Source.build("alpha", "A", "https://a.example/v1"))
    catalog = catalog.add_source(Source.build("beta", "B", "https://b.example/v1"))
    catalog = catalog.publish("alpha", ["shared", "alpha-only"])
    catalog = catalog.publish("beta", ["shared", "beta-only"])
    return SourceRouter(catalog, {"alpha": ["ka"], "beta": ["kb"]},
                        sticky_capacity=sticky_capacity)


class StickinessTests(unittest.TestCase):
    def test_session_is_remembered(self):
        router = _router()
        router.bind_session("conv-1", "shared")
        self.assertEqual(router.sticky_source("conv-1"), "alpha")

    def test_unknown_session_reports_empty(self):
        self.assertEqual(_router().sticky_source("never-seen"), "")

    def test_namespaced_model_records_its_source(self):
        router = _router()
        router.bind_session("conv-2", "beta/beta-only")
        self.assertEqual(router.sticky_source("conv-2"), "beta")

    def test_unrouted_model_binds_nothing(self):
        router = _router()
        router.bind_session("conv-3", "does-not-exist")
        self.assertEqual(router.sticky_source("conv-3"), "")

    def test_empty_session_is_ignored(self):
        router = _router()
        router.bind_session("", "shared")
        self.assertEqual(router._sticky, {})

    def test_sticky_table_is_bounded(self):
        router = _router(sticky_capacity=32)
        for index in range(500):
            router.bind_session(f"conv-{index}", "shared")
        self.assertLessEqual(len(router._sticky), 32,
                             "an unbounded session table is a memory leak")

    def test_rebinding_updates_the_choice(self):
        router = _router()
        router.bind_session("conv-4", "shared")
        router.bind_session("conv-4", "beta/beta-only")
        self.assertEqual(router.sticky_source("conv-4"), "beta")


if __name__ == "__main__":
    unittest.main()
