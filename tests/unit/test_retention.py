from __future__ import annotations
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from popo.storage.retention import prune_dir, prune_jsonl, prune_records


def _iso(days_ago: float) -> str:
    stamp = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="popo_retention_")

    def _write(self, path, rows):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_drops_old_entries_and_keeps_recent(self):
        path = os.path.join(self.tmp, "logs", "requests.jsonl")
        self._write(path, [{"ts": _iso(30), "v": "old"}, {"ts": _iso(0.1), "v": "new"}])
        report = prune_jsonl(path, days=7)
        self.assertEqual(report.dropped_lines, 1)
        self.assertEqual(report.kept_lines, 1)
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn('"new"', content)
        self.assertNotIn('"old"', content)

    def test_keeps_unparseable_lines(self):
        path = os.path.join(self.tmp, "logs", "requests.jsonl")
        self._write(path, [{"ts": _iso(30), "v": "old"}])
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        report = prune_jsonl(path, days=7)
        self.assertEqual(report.dropped_lines, 1)
        with open(path, encoding="utf-8") as handle:
            self.assertIn("{not json", handle.read())

    def test_no_rewrite_when_nothing_expired(self):
        path = os.path.join(self.tmp, "logs", "requests.jsonl")
        self._write(path, [{"ts": _iso(0.1)}])
        report = prune_jsonl(path, days=7)
        self.assertEqual(report.rewritten_files, 0)

    def test_missing_file_is_not_an_error(self):
        report = prune_jsonl(os.path.join(self.tmp, "nope.jsonl"), days=7)
        self.assertEqual(report.errors, [])

    def test_prune_dir_deletes_old_daily_traces(self):
        trace_dir = os.path.join(self.tmp, "runtime", "trace")
        os.makedirs(trace_dir)
        old = os.path.join(trace_dir, "20200101.jsonl")
        new = os.path.join(trace_dir, "20990101.jsonl")
        for path in (old, new):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}\n")
        os.utime(old, (time.time() - 30 * 86400,) * 2)
        report = prune_dir(trace_dir, days=7)
        self.assertEqual(report.deleted_files, 1)
        self.assertTrue(os.path.exists(new))
        self.assertFalse(os.path.exists(old))

    def test_whole_store_prune(self):
        self._write(os.path.join(self.tmp, "state", "usage.jsonl"),
                    [{"ts": _iso(60)}, {"ts": _iso(1)}])
        report = prune_records(self.tmp, days=7)
        self.assertEqual(report.dropped_lines, 1)
        self.assertEqual(report.errors, [])


if __name__ == "__main__":
    unittest.main()
