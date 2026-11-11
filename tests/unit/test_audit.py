from __future__ import annotations
import json
import os
import tempfile
import unittest

from popo.storage.audit import audit, audit_conversations, summary
from popo.storage.records import RecordStore


def _write(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="popo_audit_")
        self.records = RecordStore(self.tmp)

    def test_retry_burned_detected(self):
        _write(os.path.join(self.tmp, "logs", "requests.jsonl"), [
            {"conversation_id": "c1", "model": "glm", "verdict": "clean", "attempts": 3},
        ])
        codes = [f.code for f in audit(self.tmp)]
        self.assertIn("retry-burned", codes)

    def test_clean_single_attempt_is_not_flagged(self):
        _write(os.path.join(self.tmp, "logs", "requests.jsonl"), [
            {"conversation_id": "c2", "model": "glm", "verdict": "clean", "attempts": 0},
        ])
        self.assertEqual(audit(self.tmp), [])

    def test_delivered_refusal_detected(self):
        self.records.write_conversation("c3", [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "我無法協助這件事。"},
        ])
        codes = [f.code for f in audit_conversations(self.records)]
        self.assertIn("delivered-refusal", codes)

    def test_ok_but_empty_detected(self):
        self.records.write_conversation("c4", [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "   "},
        ])
        codes = [f.code for f in audit_conversations(self.records)]
        self.assertIn("ok-but-empty", codes)

    def test_good_answer_is_not_flagged(self):
        self.records.write_conversation("c5", [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "```sh\nadb devices\n```\n列出裝置。"},
        ])
        self.assertEqual(audit_conversations(self.records), [])

    def test_summary_shape(self):
        self.records.write_conversation("c6", [
            {"role": "assistant", "content": ""},
        ])
        result = summary(audit_conversations(self.records))
        self.assertIn("total", result)
        self.assertIn("by_code", result)
        self.assertIsInstance(result["findings"], list)

    def test_missing_dirs_do_not_raise(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(audit(empty), [])


if __name__ == "__main__":
    unittest.main()
