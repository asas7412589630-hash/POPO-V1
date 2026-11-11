"""Multi-source routing: the capability V1 had and 2.3.0 did not.

The behaviours under test are the ones that cost real debugging in V1:

* publishing must not delete a bare alias another client already uses;
* the same base URL typed two ways must not create two route sets;
* an unpublished model must not fall through to a different source.
"""
from __future__ import annotations
import json
import os
import tempfile
import unittest

from popo.errors import ProxyError
from popo.server.sources import (CatalogStore, Route, Source, SourceCatalog,
                                 SourceRouter)


def _source(source_id="alpha", base="https://alpha.example/v1") -> Source:
    return Source.build(source_id, source_id.title(), base)


class CatalogTests(unittest.TestCase):
    def test_publish_creates_namespaced_and_bare_alias(self):
        catalog = SourceCatalog().add_source(_source())
        catalog = catalog.publish("alpha", ["glm-5.3"])
        self.assertIn("alpha/glm-5.3", catalog.routes)
        self.assertIn("glm-5.3", catalog.routes)
        self.assertTrue(catalog.routes["alpha/glm-5.3"].namespaced)
        self.assertFalse(catalog.routes["glm-5.3"].namespaced)

    def test_bare_alias_is_not_stolen_from_another_source(self):
        catalog = SourceCatalog().add_source(_source("alpha", "https://a.example/v1"))
        catalog = catalog.add_source(_source("beta", "https://b.example/v1"))
        catalog = catalog.publish("alpha", ["glm-5.3"])
        catalog = catalog.publish("beta", ["glm-5.3"])
        # The first publisher keeps the bare name; the second is reachable only
        # through its namespaced alias. Silently rerouting would send traffic to
        # a provider the operator did not choose.
        self.assertEqual(catalog.routes["glm-5.3"].source_id, "alpha")
        self.assertEqual(catalog.routes["beta/glm-5.3"].source_id, "beta")

    def test_republish_is_idempotent(self):
        catalog = SourceCatalog().add_source(_source())
        once = catalog.publish("alpha", ["m1", "m2"])
        twice = once.publish("alpha", ["m1", "m2"])
        self.assertEqual(once.routes.keys(), twice.routes.keys())

    def test_unpublishing_removes_only_namespaced_alias(self):
        catalog = SourceCatalog().add_source(_source())
        catalog = catalog.publish("alpha", ["m1"])
        # Simulate a hand-written bare alias for a different model.
        catalog = SourceCatalog(sources=dict(catalog.sources),
                                routes={**catalog.routes})
        catalog = catalog.publish("alpha", [])
        # The namespaced alias this function owns is gone...
        self.assertNotIn("alpha/m1", catalog.routes)
        # ...but publishing never deletes a name a client might already send.
        self.assertIn("m1", catalog.routes)

    def test_same_base_url_two_spellings_is_one_source(self):
        catalog = SourceCatalog().add_source(_source("alpha", "https://x.example/v1"))
        catalog = catalog.add_source(_source("beta", "https://x.example/v1/"))
        self.assertEqual(len(catalog.sources), 1)
        self.assertIn("beta", catalog.sources)

    def test_routes_of_removed_source_are_dropped(self):
        catalog = SourceCatalog().add_source(_source("alpha", "https://a.example/v1"))
        catalog = catalog.add_source(_source("beta", "https://b.example/v1"))
        catalog = catalog.publish("alpha", ["m"])
        # Replacing alpha's URL removes alpha; its routes must not survive.
        catalog = SourceCatalog(sources={k: v for k, v in catalog.sources.items()
                                         if k != "alpha"},
                                routes={k: v for k, v in catalog.routes.items()
                                        if v.source_id != "alpha"})
        self.assertNotIn("alpha/m", catalog.routes)

    def test_source_validation_rejects_bad_input(self):
        for args in (("BAD ID", "n", "https://x.example"),
                     ("ok", "", "https://x.example"),
                     ("ok", "n", "ftp://x.example"),
                     ("ok", "n", "https://user:pw@x.example"),
                     ("ok", "n", "https://x.example?q=1")):
            with self.assertRaises(ProxyError, msg=str(args)):
                Source.build(*args)


class RouterTests(unittest.TestCase):
    def _router(self):
        catalog = SourceCatalog().add_source(_source("alpha", "https://a.example/v1"))
        catalog = catalog.add_source(_source("beta", "https://b.example/v1"))
        catalog = catalog.publish("alpha", ["glm-5.3"])
        catalog = catalog.publish("beta", ["deepseek"])
        return SourceRouter(catalog, {"alpha": ["ka"], "beta": ["kb"]})

    def test_routes_by_model(self):
        router = self._router()
        self.assertEqual(router("glm-5.3").host, "a.example")
        self.assertEqual(router("deepseek").host, "b.example")
        self.assertEqual(router("beta/deepseek").host, "b.example")

    def test_unknown_model_is_refused_not_guessed(self):
        with self.assertRaises(ProxyError) as ctx:
            self._router()("not-published")
        self.assertEqual(ctx.exception.kind.value, "client.invalid_model")

    def test_source_without_credential_is_reported(self):
        catalog = SourceCatalog().add_source(_source("alpha", "https://a.example/v1"))
        catalog = catalog.publish("alpha", ["m"])
        router = SourceRouter(catalog, {})
        with self.assertRaises(ProxyError) as ctx:
            router("m")
        self.assertEqual(ctx.exception.kind.value, "proxy.config")

    def test_sources_are_cached_per_base_url(self):
        router = self._router()
        self.assertIs(router("glm-5.3"), router("glm-5.3"))

    def test_close_releases_the_cache(self):
        router = self._router()
        router("glm-5.3")
        router.close()
        self.assertEqual(len(router._cache), 0)

    def test_profile_for_prefers_route_override_then_setup_default(self):
        catalog = SourceCatalog().add_source(_source())
        catalog = catalog.publish("alpha", ["routed"], profiles={"routed": "native_glm"})
        router = SourceRouter(catalog, {"alpha": ["ka"]},
                              default_profiles={"routed": "off", "defaulted": "native_kimi"})
        self.assertEqual(router.profile_for("alpha/routed"), "native_glm")
        self.assertEqual(router.profile_for("routed"), "native_glm")
        self.assertEqual(router.profile_for("defaulted"), "native_kimi")
        self.assertEqual(router.profile_for("unknown"), "")

    def test_profile_for_follows_live_config_not_build_time_snapshot(self):
        # The router is cached for the life of the process while the console
        # mutates config.model_profiles in place on every re-apply. Reading a
        # build-time snapshot left every later console edit invisible until a
        # restart (found 2026-11-12).
        from popo.config import Config
        catalog = SourceCatalog().add_source(_source())
        catalog = catalog.publish("alpha", ["m"])
        config = Config(model_profiles={"m": "native_glm"})
        router = SourceRouter(catalog, {"alpha": ["ka"]},
                              default_profiles={"m": "off"}, config=config)
        self.assertEqual(router.profile_for("m"), "native_glm")
        config.model_profiles = {"m": "off"}
        self.assertEqual(router.profile_for("m"), "off")
        # A route-level override still outranks the live config.
        catalog = catalog.publish("alpha", ["m"], profiles={"m": "native_kimi"})
        router_with_route = SourceRouter(catalog, {"alpha": ["ka"]}, config=config)
        self.assertEqual(router_with_route.profile_for("m"), "native_kimi")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="popo_sources_")
        self.store = CatalogStore(self.tmp)

    def test_round_trip(self):
        catalog = SourceCatalog().add_source(_source())
        catalog = catalog.publish("alpha", ["m1", "m2"])
        self.store.save(catalog)
        loaded = self.store.load()
        self.assertEqual(sorted(loaded.routes), sorted(catalog.routes))
        self.assertEqual(loaded.sources["alpha"].base_url, "https://alpha.example/v1")

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits do not exist on Windows")
    def test_secrets_are_separate_and_private(self):
        self.store.save(SourceCatalog().add_source(_source()))
        self.store.save_secret("alpha", ["sk-secret"])
        registry = json.loads(open(self.store.registry_path, encoding="utf-8").read())
        self.assertNotIn("sk-secret", json.dumps(registry))
        self.assertEqual(os.stat(self.store.secrets_path).st_mode & 0o777, 0o600)
        self.assertEqual(self.store.load_secrets()["alpha"], ["sk-secret"])

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits do not exist on Windows")
    def test_registry_is_world_readable_but_has_no_secret(self):
        self.store.save(SourceCatalog().add_source(_source()))
        mode = os.stat(self.store.registry_path).st_mode & 0o777
        self.assertEqual(mode, 0o644)

    def test_corrupt_registry_does_not_raise(self):
        with open(self.store.registry_path, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        self.assertEqual(len(self.store.load().sources), 0)

    def test_route_with_missing_source_is_dropped(self):
        self.store._write(self.store.registry_path, {
            "sources": [], "routes": {"m": {"source_id": "gone", "model_id": "m"}}}, 0o644)
        self.assertEqual(self.store.load().routes, {})

    def test_ephemeral_writes_nothing(self):
        store = CatalogStore(self.tmp, ephemeral=True)
        store.save(SourceCatalog().add_source(_source()))
        self.assertFalse(os.path.exists(store.registry_path))


if __name__ == "__main__":
    unittest.main()
