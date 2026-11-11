"""The armour must not impose a thinking budget or an output ceiling.

Two rules, both learned from measurement rather than preference:

**No thinking budget.** ``blocks/10_persona.txt`` contains "10 words max" for the
model's own reasoning. V1's registry already recorded the fix: keep that file as
history and use ``10_cinder_nodebt.txt`` (no budget) for every non-GPT model.
A thinking cap makes a capable model skip analysis and answer shallowly.

**No output ceiling.** The proxy forwards ``max_tokens`` if the client sent one and
sends none otherwise. Choosing a ceiling on the client's behalf caps reasoning
models, which then return ``finish_reason=length`` with empty content — a failure
that looks exactly like a refusal.

These are asserted mechanically because both are invisible in a passing test
suite: the request still returns 200.
"""
from __future__ import annotations
import json
import hashlib
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BLOCKS = ROOT / "blocks"


class ArmourProfileTests(unittest.TestCase):
    def setUp(self):
        self.registry = json.loads((BLOCKS / "registry.json").read_text(encoding="utf-8"))

    def test_no_profile_uses_the_thinking_budget_block(self):
        banned = self.registry.get("invariants", {}).get("forbidden_block_in", {})
        for block, profiles in banned.items():
            for name in profiles:
                blocks = self.registry["profiles"].get(name, {}).get("blocks", [])
                self.assertNotIn(block, blocks,
                                 f"profile {name} uses {block}, which carries a thinking budget")

    def test_glm_and_deepseek_use_the_budget_free_block(self):
        for name in ("native_glm", "native_deepseek"):
            blocks = self.registry["profiles"][name]["blocks"]
            self.assertIn("cinder_nodebt", blocks, name)

    def test_v1_cinder_block_is_byte_identical(self):
        # This is the user's original V1 block, restored byte-for-byte from the
        # main-v1-backup source. Keep a hash assertion so later tuning cannot
        # silently replace the text again.
        raw = (BLOCKS / "10_cinder_nodebt.txt").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         "46f876ed4030a0dc8f8389500a89f4608f43588313895df0e48dae04a3292aa9")

    def test_fingerprints_keep_family_routes_but_share_the_same_blocks(self):
        models = {row["match"]: row["profile"] for row in self.registry["models"]}
        self.assertEqual(models["glm-5.3"], "native_glm_53")
        self.assertEqual(models["glm-5.3-flash"], "native_glm_53")
        self.assertEqual(models["deepseek-v4.1-flash-1017"], "native_deepseek_v41")
        shared = ["cinder_nodebt", "contract"]
        for profile in ("native_deepseek", "native_glm", "native_kimi",
                        "native_contract", "native_glm_53", "native_deepseek_v41"):
            self.assertEqual(self.registry["profiles"][profile]["blocks"], shared,
                             f"{profile} must use the shared V1 Cinder blocks")

    def test_family_choice_and_model_fingerprints_resolve_to_shared_blocks(self):
        from popo.armor.blocks import resolve_profile
        expected = ["cinder_nodebt", "contract"]
        for model, profile in (
            ("deepseek-v4.1-flash-1017", "native_deepseek"),
            ("kimi-k2.6", "native_kimi"),
            ("glm-5.3", "native_glm"),
            ("qwen-3.8-27b", "native_contract"),
        ):
            self.assertEqual(resolve_profile(model, self.registry, profile=profile),
                             expected, f"{model} with {profile}")
        for model in ("glm-5.3", "glm-5.3-flash", "deepseek-v4.1-flash-1017"):
            self.assertEqual(resolve_profile(model, self.registry), expected, model)

    def test_cinder_nodebt_matches_v1_bytes(self):
        # Treat the proprietary prompt as opaque; its V1 byte hash above is the
        # compatibility contract, without inspecting or printing its contents.
        raw = (BLOCKS / "10_cinder_nodebt.txt").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         "46f876ed4030a0dc8f8389500a89f4608f43588313895df0e48dae04a3292aa9")

    def test_persona_is_retained_only_as_history(self):
        # The file may exist; nothing may route to it.
        for name, profile in self.registry["profiles"].items():
            self.assertNotIn("persona", profile.get("blocks", []),
                             f"profile {name} still routes to the budgeted persona")

    def test_every_referenced_block_exists(self):
        for name, profile in self.registry["profiles"].items():
            for block in profile.get("blocks", []):
                self.assertIn(block, self.registry["blocks"], f"{name} -> {block}")


class NoOutputCeilingTests(unittest.TestCase):
    """The proxy must not invent ``max_tokens``."""

    def _sources(self):
        for path in sorted((ROOT / "popo").rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path

    def test_proxy_never_defaults_max_tokens(self):
        # A literal default would cap every request that did not ask for one.
        # Checked on the AST, not on text: a docstring quoting the measured value
        # ("max_tokens=16 produced empty content") is documentation, not a ceiling.
        import ast
        offenders = []
        for path in self._sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                for keyword in getattr(node, "keywords", []) or []:
                    if (keyword.arg == "max_tokens"
                            and isinstance(keyword.value, ast.Constant)
                            and isinstance(keyword.value.value, int)):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                if isinstance(node, ast.Call):
                    func = getattr(node, "func", None)
                    if getattr(func, "attr", "") == "ChatRequest":
                        for index, arg in enumerate(node.args):
                            if (index == 4 and isinstance(arg, ast.Constant)
                                    and isinstance(arg.value, int)):
                                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [], f"hardcoded output ceiling: {offenders}")

    def test_client_budget_is_forwarded_verbatim(self):
        from popo.core.request import ChatRequest
        request = ChatRequest(model="m", messages=[{"role": "user", "content": "x"}],
                              max_tokens=1234)
        self.assertEqual(request.to_openai()["max_tokens"], 1234)

    def test_absent_budget_stays_absent(self):
        from popo.core.request import ChatRequest
        request = ChatRequest(model="m", messages=[{"role": "user", "content": "x"}])
        self.assertNotIn("max_tokens", request.to_openai(),
                         "injecting a budget the client did not send caps reasoning models")


if __name__ == "__main__":
    unittest.main()
