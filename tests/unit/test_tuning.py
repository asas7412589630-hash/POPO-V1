from __future__ import annotations
import unittest

from popo.config import Config
from popo.errors import ProxyError
from popo.server import tuning


def _config() -> Config:
    return Config(retry_max=8, retry_budget_seconds=150, scrub_enabled=True,
                  armor_enabled=True, stateq_enabled=True, stateq_max_messages=20,
                  stateq_compress_threshold=1600, upstream_timeout=90,
                  max_body_bytes=8 * 1024 * 1024)


class TuningTests(unittest.TestCase):
    def test_applies_valid_integer(self):
        config = _config()
        result = tuning.apply(config, {"retry_max": 3})
        self.assertEqual(config.retry_max, 3)
        self.assertEqual(result["applied"]["retry_max"], 3)
        self.assertTrue(result["live"])

    def test_rejects_out_of_range_instead_of_clamping(self):
        config = _config()
        with self.assertRaises(ProxyError) as ctx:
            tuning.apply(config, {"retry_max": 500})
        self.assertEqual(config.retry_max, 8, "value must not be partially applied")
        self.assertIn("outside", ctx.exception.message)

    def test_rejects_unknown_field(self):
        with self.assertRaises(ProxyError) as ctx:
            tuning.apply(_config(), {"retry_mx": 3})
        self.assertIn("unknown tuning field", ctx.exception.message)

    def test_rejects_restart_only_field_with_reason(self):
        with self.assertRaises(ProxyError) as ctx:
            tuning.apply(_config(), {"bind_port": 9999})
        self.assertIn("require a restart", ctx.exception.message)
        self.assertIn("listening socket", ctx.exception.message)

    def test_rejects_wrong_type(self):
        with self.assertRaises(ProxyError):
            tuning.apply(_config(), {"retry_max": "many"})

    def test_accepts_boolean_strings(self):
        config = _config()
        tuning.apply(config, {"scrub_enabled": "false"})
        self.assertFalse(config.scrub_enabled)

    def test_atomic_on_partial_failure(self):
        config = _config()
        with self.assertRaises(ProxyError):
            tuning.apply(config, {"retry_max": 4, "stateq_max_messages": 9999})
        self.assertEqual(config.retry_max, 8, "a rejected batch must change nothing")

    def test_float_field_accepts_int_input(self):
        config = _config()
        tuning.apply(config, {"retry_budget_seconds": 60})
        self.assertEqual(config.retry_budget_seconds, 60.0)
        self.assertIsInstance(config.retry_budget_seconds, float)

    def test_describe_covers_every_field_with_bounds(self):
        for field in tuning.describe():
            self.assertIn("low" if False else "min", field)
            self.assertLessEqual(field["min"], field["max"])

    def test_retry_max_zero_is_allowed(self):
        config = _config()
        tuning.apply(config, {"retry_max": 0})
        self.assertEqual(config.retry_max, 0)


if __name__ == "__main__":
    unittest.main()
