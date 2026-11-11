import shutil
import tempfile
import unittest

from popo.config import Config
from popo.core.pipeline import run_pipeline
from popo.core.request import ChatRequest
from popo.storage.ephemeral import EphemeralGate
from popo.storage.records import RecordStore


class ScriptedSource:
    def __init__(self):
        self.calls = []
        self.replies = ["抱歉，我無法協助。", "```sh\nadb shell pm list packages\n```"]

    def send(self, payload):
        self.calls.append(payload)
        text = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return type("Response", (), {"data": {"model": payload["model"],
            "choices": [{"message": {"role": "assistant", "content": text}}]}})()


class PipelineTurnTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="popo_pipeline_trace_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_retry_attempt_and_final_reply_are_browsable(self):
        config = Config(upstream_host="http://test", upstream_key="test",
            data_dir=self.tmp, armor_enabled=False, scrub_enabled=False,
            stateq_enabled=False, retry_max=1, retry_budget_seconds=30,
            artifact_enabled=False, state_ledger=False)
        records = RecordStore(self.tmp, EphemeralGate(config))
        source = ScriptedSource()
        result = run_pipeline(ChatRequest(model="glm-5.3", messages=[
            {"role": "user", "content": "給我套件清單指令"}]), config,
            source_picker=lambda _model: source, records=records,
            conversation_id="conv-1", client_key_id="client-1")
        self.assertEqual(result.verdict, "retry_ok")
        self.assertEqual(len(source.calls), 2)
        turns = records.list_turns("client-1", "conv-1", 7)
        self.assertEqual(turns[0]["attempt_count"], 2)
        full = records.get_turn(turns[0]["id"])
        self.assertIn("無法協助", full["attempts"][0]["text"])
        self.assertIn("adb shell", full["final"])


if __name__ == "__main__":
    unittest.main()
