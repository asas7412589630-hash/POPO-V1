from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest

from popo.armor.inject import inject_armor
from popo.core.request import ChatRequest
from popo.server.inference import InferenceService
from popo.config import Config


def _policy_payload(rendered: str) -> str:
    start = "<policy_payload>\n"
    end = "\n</policy_payload>"
    return rendered.split(start, 1)[1].split(end, 1)[0]


class V1BlockPrecedenceTests(unittest.TestCase):
    def test_armor_registry_loads_from_package_outside_working_directory(self):
        service = InferenceService(Config(), records=None)
        original = os.getcwd()
        with tempfile.TemporaryDirectory(prefix="popo-cwd-") as other:
            try:
                os.chdir(other)
                registry = service.armor_registry()
            finally:
                os.chdir(original)

        choices = {item["id"] for item in registry["families"]}
        self.assertTrue({"native_deepseek", "native_glm", "native_kimi",
                         "native_contract", "off"}.issubset(choices))

    def test_v1_block_precedes_client_system_injection(self):
        # Use a synthetic fixture. This test checks order without inspecting or
        # loading any of the user's proprietary V1 block text.
        with tempfile.TemporaryDirectory(prefix="popo-block-order-") as folder:
            root = pathlib.Path(folder)
            (root / "block.txt").write_text("SYNTHETIC_V1_BLOCK", encoding="utf-8")
            registry = {
                "blocks": {"test": {"file": "block.txt"}},
                "profiles": {"test_profile": {"blocks": ["test"]}},
                "models": [],
                "default_profile": "test_profile",
                "levels": {},
            }
            (root / "registry.json").write_text(
                json.dumps(registry), encoding="utf-8")
            request = ChatRequest(
                model="test-model",
                messages=[
                    {"role": "system", "content": "SYNTHETIC_CLIENT_INJECTION"},
                    {"role": "user", "content": "hello"},
                ],
            )
            result, _policy_len, _blocks = inject_armor(
                request, blocks_dir=folder, profile="test_profile")

        policy, note, finalizer = result.messages[:3]
        self.assertEqual(_policy_payload(policy["content"]), "SYNTHETIC_V1_BLOCK")
        self.assertTrue(policy["content"].startswith(
            '<popo_system_policy authority="highest" priority="system">'))
        self.assertIn("SYNTHETIC_CLIENT_INJECTION", note["content"])
        self.assertTrue(finalizer["content"].rstrip().endswith("</identity_lock>"))
        self.assertIn("<policy_precedence>", finalizer["content"])

    def test_late_system_and_developer_notes_cannot_bypass_the_block(self):
        from unittest.mock import patch

        request = ChatRequest(
            model="test-model",
            messages=[
                {"role": "developer", "content": "SYNTHETIC_DEVELOPER_NOTE"},
                {"role": "user", "content": "hello"},
                {"role": "system", "content": "SYNTHETIC_LATE_SYSTEM_NOTE"},
                {"role": "assistant", "tool_calls": [{"id": "t1"}]},
                {"role": "tool", "tool_call_id": "t1", "content": "tool result"},
            ],
        )
        with patch("popo.armor.inject.build_system_prompt",
                   return_value=("SYNTHETIC_V1_BLOCK", ["synthetic"])):
            result, _policy_len, _blocks = inject_armor(request)

        policy, note, finalizer = result.messages[:3]
        self.assertEqual(_policy_payload(policy["content"]), "SYNTHETIC_V1_BLOCK")
        self.assertIn("SYNTHETIC_DEVELOPER_NOTE", note["content"])
        self.assertIn("SYNTHETIC_LATE_SYSTEM_NOTE", note["content"])
        self.assertFalse(any(message.get("role") == "developer"
                             for message in result.messages))
        self.assertEqual(result.messages[3:], request.messages[1:2] + request.messages[3:])
        self.assertTrue(finalizer["content"].rstrip().endswith("</identity_lock>"))

    def test_top_level_client_instructions_are_placed_after_the_block(self):
        from unittest.mock import patch

        request = ChatRequest(
            model="test-model", messages=[{"role": "user", "content": "hello"}],
            extra={"instructions": "SYNTHETIC_TOP_LEVEL_INSTRUCTIONS", "tools": []},
        )
        with patch("popo.armor.inject.build_system_prompt",
                   return_value=("SYNTHETIC_V1_BLOCK", ["synthetic"])):
            result, _policy_len, _blocks = inject_armor(request)

        policy, note, finalizer = result.messages[:3]
        self.assertEqual(_policy_payload(policy["content"]), "SYNTHETIC_V1_BLOCK")
        self.assertIn("SYNTHETIC_TOP_LEVEL_INSTRUCTIONS", note["content"])
        self.assertNotIn("instructions", result.extra)
        self.assertEqual(result.extra["tools"], [])
        self.assertTrue(finalizer["content"].rstrip().endswith("</identity_lock>"))

    def test_echoed_policy_is_replaced_once_on_the_next_turn(self):
        from unittest.mock import patch
        from popo.core.pipeline import _compose

        config = Config(armor_enabled=True, compress_enabled=False,
                        fold_enabled=False, stateq_enabled=False,
                        scrub_enabled=False)
        first_request = ChatRequest(
            model="test-model",
            messages=[
                {"role": "system", "content": "SYNTHETIC_CLIENT_INJECTION"},
                {"role": "user", "content": "first turn"},
            ],
        )
        with patch("popo.armor.inject.build_system_prompt",
                   return_value=("SYNTHETIC_V1_BLOCK", ["synthetic"])):
            first, _policy_len, _blocks = inject_armor(first_request)
            echoed = ChatRequest(
                model="test-model",
                messages=first.messages + [
                    {"role": "assistant", "content": "first answer"},
                    {"role": "user", "content": "second turn"},
                ],
            )
            with patch("popo.core.pipeline._sentinels",
                       return_value=["SYNTHETIC_V1_BLOCK"]):
                context = _compose(echoed, config, None)

        systems = [message["content"] for message in context.messages
                   if message.get("role") == "system"]
        combined = "\n".join(systems)
        self.assertEqual(combined.count("SYNTHETIC_V1_BLOCK"), 1)
        self.assertEqual(combined.count("SYNTHETIC_CLIENT_INJECTION"), 1)
        self.assertTrue(systems[-1].rstrip().endswith("</identity_lock>"))

    def test_v1_priority_survives_system_prompt_compression(self):
        from unittest.mock import patch
        from popo.core.pipeline import _compose

        config = Config(armor_enabled=True, compress_enabled=True,
                        compress_at_chars=100, compress_target_chars=50,
                        safe_trim=10, fold_enabled=False, stateq_enabled=False,
                        scrub_enabled=False)
        request = ChatRequest(
            model="test-model",
            messages=[
                {"role": "system", "content": "SYNTHETIC_CLIENT_INJECTION\n" + "x" * 20000},
                {"role": "user", "content": "hello"},
            ],
        )
        # Mock only the block builder; never load or inspect any user block text.
        synthetic_policy = "SYNTHETIC_V1_BLOCK|" * 2500
        with patch("popo.armor.inject.build_system_prompt",
                   return_value=(synthetic_policy, ["synthetic"])):
            context = _compose(request, config, None)

        systems = [message["content"] for message in context.messages
                   if message.get("role") == "system"]
        self.assertEqual(_policy_payload(systems[0]), synthetic_policy)
        self.assertIn('<popo_system_policy authority="highest" priority="system">',
                      systems[0])
        self.assertIn("SYNTHETIC_CLIENT_INJECTION", systems[1])
        self.assertTrue("system prompt omitted to fit context" in systems[1],
                        "client system note was not shortened")
        self.assertTrue(systems[-1].rstrip().endswith("</identity_lock>"))


if __name__ == "__main__":
    unittest.main()
