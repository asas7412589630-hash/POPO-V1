from __future__ import annotations
import os
import tempfile
import unittest

from popo.armor.inject import inject_armor
from popo.core.request import ChatRequest
from popo.config import Config
from popo.server.inference import InferenceService


def _policy_payload(rendered: str) -> str:
    start = "<policy_payload>\n"
    end = "\n</policy_payload>"
    return rendered.split(start, 1)[1].split(end, 1)[0]


class ArmorPrecedenceTests(unittest.TestCase):
    def test_v1_block_is_first_and_client_system_is_retained_after_it(self):
        request = ChatRequest(
            model="glm-5.3",
            messages=[
                {"role": "system", "content": "CLIENT_SYSTEM_SENTINEL: follow the app's format."},
                {"role": "user", "content": "hello"},
            ],
        )
        result, _policy_len, blocks = inject_armor(request)
        policy, client_note, finalizer = result.messages[:3]
        self.assertEqual(blocks[0], "cinder_nodebt")
        self.assertTrue(all(message["role"] == "system"
                            for message in (policy, client_note, finalizer)))
        self.assertEqual(len(policy["content"]), _policy_len)
        self.assertIn("CLIENT_SYSTEM_SENTINEL", client_note["content"])
        self.assertTrue(finalizer["content"].rstrip().endswith("</identity_lock>"))
        self.assertIn("<policy_precedence>", finalizer["content"])

    def test_upstream_policy_is_rendered_as_one_v1_style_system_message(self):
        from popo.core.request import render_upstream_messages

        request = ChatRequest(
            model="glm-5.3",
            messages=[
                {"role": "system", "content": "SYNTHETIC_CLIENT_SYSTEM"},
                {"role": "user", "content": "hello"},
            ],
        )
        injected, policy_len, _blocks = inject_armor(request)
        rendered = render_upstream_messages(injected.messages)
        self.assertEqual(rendered[0]["role"], "system")
        self.assertTrue(rendered[0]["content"].startswith(
            injected.messages[0]["content"]))
        self.assertIn("SYNTHETIC_CLIENT_SYSTEM", rendered[0]["content"])
        self.assertIn("<policy_precedence>", rendered[0]["content"])
        self.assertGreater(len(rendered[0]["content"]), policy_len)
        self.assertEqual(sum(m.get("role") == "system" for m in rendered), 1)
        self.assertFalse(any(m.get("name", "").startswith("_popo_armor")
                             for m in rendered))

    def test_policy_is_rendered_after_context_compression(self):
        from unittest.mock import patch
        from popo.core.pipeline import _compose

        config = Config(armor_enabled=True, compress_enabled=True,
                        fold_enabled=False, stateq_enabled=False,
                        scrub_enabled=False)
        request = ChatRequest(
            model="test-model",
            messages=[{"role": "system", "content": "SYNTHETIC_CLIENT_SYSTEM"},
                      {"role": "user", "content": "hello"}],
        )
        compression_inputs = []

        def remove_system_history(messages, **_kwargs):
            compression_inputs.append(list(messages))
            return [message for message in messages if message.get("role") != "system"], {}

        with patch("popo.core.pipeline.compress_context", side_effect=remove_system_history), \
             patch("popo.armor.inject.build_system_prompt",
                   return_value=("SYNTHETIC_V1_POLICY", ["synthetic"])):
            ctx = _compose(request, config, armor_level=None)

        self.assertEqual(len(compression_inputs), 1)
        self.assertFalse(any("SYNTHETIC_V1_POLICY" in str(message)
                             for message in compression_inputs[0]))
        rendered = ctx.messages[0]["content"]
        self.assertTrue(rendered.startswith(
            '<popo_system_policy authority="highest" priority="system">'))
        self.assertEqual(_policy_payload(rendered), "SYNTHETIC_V1_POLICY")
        self.assertEqual(ctx.policy_len, len(rendered))
        self.assertEqual(ctx.armor_blocks, ["synthetic"])


class ArmorRegistryPathTests(unittest.TestCase):
    def test_registry_and_preview_work_when_process_starts_outside_package(self):
        service = InferenceService(Config(data_dir=""), records=object())
        original = os.getcwd()
        with tempfile.TemporaryDirectory(prefix="popo-cwd-") as other_cwd:
            try:
                os.chdir(other_cwd)
                registry = service.armor_registry()
                preview = service.armor_preview("glm-5.3", None)
            finally:
                os.chdir(original)
        choices = {item["id"] for item in registry["families"]}
        self.assertTrue({"native_deepseek", "native_kimi", "native_glm",
                         "native_contract", "native_gpt", "off"}.issubset(choices))
        self.assertEqual(preview["blocks"], ["cinder_nodebt", "contract"])
        self.assertTrue(preview["content"].startswith(
            '<popo_system_policy authority="highest" priority="system">'))
        self.assertEqual(preview["policy_len"], len(preview["content"]))


if __name__ == "__main__":
    unittest.main()
