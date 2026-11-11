"""Every declared route must have an implementation, and its auth must be real.

This is a guard against the failure mode where a route is added to the table and
nobody notices the handler is missing — the client gets a 501 only when someone
happens to call it.
"""
from __future__ import annotations
import unittest

from popo.config import Config
from popo.server.app import ApiHandler
from popo.server.routes import CLIENT, OPERATOR, PUBLIC, ROUTES, authorize, match_route
from popo.auth.gate import Gate
from popo.errors import ProxyError


class RouteWiringTests(unittest.TestCase):
    def test_every_route_has_a_handler(self):
        missing = [route.name for route in ROUTES
                   if not hasattr(ApiHandler, f"_route_{route.name}")]
        self.assertEqual(missing, [])

    def test_no_duplicate_route_names(self):
        names = [route.name for route in ROUTES]
        self.assertEqual(len(names), len(set(names)))

    def test_no_duplicate_method_path_pairs(self):
        pairs = [(route.method, route.path) for route in ROUTES]
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_every_route_declares_a_known_auth_level(self):
        for route in ROUTES:
            self.assertIn(route.auth, (PUBLIC, CLIENT, OPERATOR), route.name)

    def test_every_admin_route_is_operator_only(self):
        for route in ROUTES:
            if route.path.startswith("/admin/"):
                self.assertEqual(route.auth, OPERATOR, route.path)

    def test_route_lookup_respects_method(self):
        self.assertIsNotNone(match_route("POST", "/admin/keys"))
        self.assertIsNone(match_route("DELETE", "/admin/keys"))

    def test_prefix_route_matches_detail_path(self):
        route = match_route("GET", "/admin/conversations/abc123")
        self.assertIsNotNone(route)
        self.assertEqual(route.name, "admin_conversation_detail")

    def test_operator_route_rejects_client_key(self):
        config = Config(require_client_key=True)
        gate = Gate(config, {"Authorization": "Bearer triad-abc"}, ("10.0.0.1", 1))
        with self.assertRaises(ProxyError):
            authorize(match_route("POST", "/admin/retention"), gate.resolve(), config)

    def test_public_route_needs_nothing(self):
        config = Config(require_client_key=True)
        gate = Gate(config, {}, ("10.0.0.1", 1))
        authorize(match_route("GET", "/healthz"), gate.resolve(), config)


if __name__ == "__main__":
    unittest.main()
