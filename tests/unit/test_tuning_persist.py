from __future__ import annotations
import json
import os
import tempfile
import unittest

from popo.config import Config
from popo.server import tuning


class TuningPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="popo_tuning_")

    def _config(self) -> Config:
        return Config(retry_max=8, scrub_enabled=True, data_dir=self.tmp)

    def test_saved_value_survives_a_restart(self):
        first = self._config()
        result = tuning.apply(first, {"retry_max": 2})
        tuning.save(self.tmp, result["applied"])

        second = self._config()
        loaded = tuning.load(second, self.tmp)
        self.assertEqual(second.retry_max, 2)
        self.assertEqual(loaded["retry_max"], 2)

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits do not exist on Windows")
    def test_file_is_private(self):
        tuning.save(self.tmp, {"retry_max": 3})
        path = os.path.join(self.tmp, tuning.TUNING_FILE)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_corrupt_file_does_not_break_startup(self):
        path = os.path.join(self.tmp, tuning.TUNING_FILE)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        config = self._config()
        self.assertEqual(tuning.load(config, self.tmp), {})
        self.assertEqual(config.retry_max, 8)

    def test_stored_out_of_range_value_is_rejected_not_applied(self):
        path = os.path.join(self.tmp, tuning.TUNING_FILE)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"retry_max": 9999}, handle)
        config = self._config()
        tuning.load(config, self.tmp)
        self.assertEqual(config.retry_max, 8)

    def test_unknown_key_in_file_is_ignored(self):
        path = os.path.join(self.tmp, tuning.TUNING_FILE)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"made_up": 1, "retry_max": 4}, handle)
        config = self._config()
        loaded = tuning.load(config, self.tmp)
        self.assertEqual(loaded, {"retry_max": 4})

    def test_save_merges_instead_of_replacing(self):
        tuning.save(self.tmp, {"retry_max": 2})
        tuning.save(self.tmp, {"scrub_enabled": False})
        config = self._config()
        tuning.load(config, self.tmp)
        self.assertEqual(config.retry_max, 2)
        self.assertFalse(config.scrub_enabled)

    def test_no_data_dir_is_a_noop(self):
        tuning.save("", {"retry_max": 2})   # must not raise


if __name__ == "__main__":
    unittest.main()
