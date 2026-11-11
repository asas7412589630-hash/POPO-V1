import unittest
from unittest.mock import MagicMock, patch

from popo.config import Config
from popo.core.pipeline import PipelineResult, run_pipeline
from popo.core.request import ChatRequest


class MockSource:
    def __init__(self, responses):
        self.responses = list(responses)
        self.idx = 0
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)
        if self.idx >= len(self.responses):
            return MagicMock(data={"choices": [{"message": {"content": "fallback"}}]})
        resp = self.responses[self.idx]
        self.idx += 1
        return MagicMock(data=resp)


class TestPipelineContext(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(
            upstream_host="http://test",
            upstream_key="fake-key",
            stateq_enabled=True,
            scrub_enabled=False,
            armor_enabled=False,
        )
        self.req = ChatRequest(
            model="glm",
            messages=[
                {"role": "system", "content": "base"},
                {"role": "user", "content": "hello"},
            ],
        )

    @patch("popo.core.pipeline._select_source")
    @patch("popo.storage.records.RecordStore")
    def test_short_conversation_uncompressed(self, mock_rs, mock_select):
        mock_select.return_value = MockSource([
            {"choices": [{"message": {"content": "hi"}}]},
        ])
        rs = mock_rs.return_value
        rs.append_request = MagicMock()
        rs.write_conversation = MagicMock()

        result = run_pipeline(self.req, self.cfg, records=rs, conversation_id="c1")
        self.assertIsInstance(result, PipelineResult)
        # 短對話不觸發壓縮，回寫含原始 messages + assistant 回覆
        written = rs.write_conversation.call_args.kwargs["messages"]
        self.assertEqual(len(written), 3)
        self.assertEqual(written[0]["content"], "base")
        self.assertEqual(written[1]["content"], "hello")

    @patch("popo.core.pipeline._select_source")
    @patch("popo.storage.records.RecordStore")
    def test_long_conversation_compresses(self, mock_rs, mock_select):
        msgs = [{"role": "system", "content": "base"}]
        for i in range(30):
            msgs.append({"role": "user", "content": f"u{i}"})
            msgs.append({"role": "assistant", "content": f"a{i}"})
        req = ChatRequest(model="glm", messages=msgs)

        mock_select.return_value = MockSource([
            {"choices": [{"message": {"content": "ok"}}]},
        ])
        rs = mock_rs.return_value
        rs.append_request = MagicMock()
        rs.write_conversation = MagicMock()

        result = run_pipeline(req, self.cfg, records=rs, conversation_id="c2")
        self.assertIsInstance(result, PipelineResult)
        written = rs.write_conversation.call_args.kwargs["messages"]
        # 回寫必須是 cleanup_context 後的結果，不能含 _popo_stateq 摘要
        names = {m.get("name") for m in written}
        self.assertNotIn("_popo_stateq", names)
        # 最後一則 user 仍保留原始內容
        contents = {m.get("content", "") for m in written if m["role"] == "user"}
        self.assertIn("u29", contents)

    @patch("popo.core.pipeline._select_source")
    @patch("popo.storage.records.RecordStore")
    def test_stateq_disabled_keeps_original(self, mock_rs, mock_select):
        cfg = Config(
            upstream_host="http://test",
            upstream_key="fake-key",
            stateq_enabled=False,
            scrub_enabled=False,
            armor_enabled=False,
        )
        msgs = [{"role": "system", "content": "base"}]
        for i in range(30):
            msgs.append({"role": "user", "content": f"u{i}"})
            msgs.append({"role": "assistant", "content": f"a{i}"})
        req = ChatRequest(model="glm", messages=msgs)

        mock_select.return_value = MockSource([
            {"choices": [{"message": {"content": "ok"}}]},
        ])
        rs = mock_rs.return_value
        rs.append_request = MagicMock()
        rs.write_conversation = MagicMock()

        result = run_pipeline(req, cfg, records=rs, conversation_id="c3")
        self.assertIsInstance(result, PipelineResult)
        written = rs.write_conversation.call_args.kwargs["messages"]
        # stateq 關閉時不壓縮；回寫 = 原始 61 則 + assistant 回覆 = 62
        self.assertEqual(len(written), 62)

    def test_selected_profile_is_applied_to_upstream_request(self):
        cfg = Config(upstream_host="http://test", upstream_key="fake-key",
                     armor_enabled=True, stateq_enabled=False, scrub_enabled=False)
        source = MockSource([{"choices": [{"message": {"content": "ok"}}]}])

        class Picker:
            def __call__(self, model):
                return source

            def profile_for(self, model):
                return "native_contract"

        records = MagicMock()
        records.get_handoff.return_value = {}
        run_pipeline(self.req, cfg, source_picker=Picker(), records=records,
                     conversation_id="profile-test")
        system = source.payloads[0]["messages"][0]["content"]
        self.assertIn("<policy runtime=", system)
        self.assertNotIn("Cinder", system)

    def test_off_profile_really_sends_no_injected_system(self):
        cfg = Config(upstream_host="http://test", upstream_key="fake-key",
                     armor_enabled=True, stateq_enabled=False, scrub_enabled=False)
        source = MockSource([{"choices": [{"message": {"content": "ok"}}]}])

        class Picker:
            def __call__(self, model):
                return source

            def profile_for(self, model):
                return "off"

        records = MagicMock()
        records.get_handoff.return_value = {}
        run_pipeline(self.req, cfg, source_picker=Picker(), records=records,
                     conversation_id="off-test")
        self.assertEqual(source.payloads[0]["messages"], self.req.messages)


if __name__ == "__main__":
    unittest.main()
